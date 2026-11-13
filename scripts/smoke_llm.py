#!/usr/bin/env python3
"""Answer one question: do the configured LLM models accept the requests we send?

Unit tests mock the SDKs, so they cannot tell when a model starts rejecting a
request param (a 400 on `temperature`, `max_tokens`, …) or stops returning tool
calls. This sends the same three request shapes the app does — `complete`, a
streamed turn (`stream_turn`), and a tool round-trip ending in `stream_turn` —
through the real providers and reports each one.

Then, on the voice turn model (issue #345): effort `low`, thinking `disabled`, a
tool loop that sends the thinking back, a tampered signature and an edited
history (both must get a 400), a model switch mid-loop (with --switch-model), a
continuation after the tool list changed (the API drops the thinking that no longer
fits), prompt-cache reads across two turns, and a forced max_tokens cut-off. On each
--openai-model: reasoning_effort `low`, thinking `disabled`, `xhigh`, and `minimal` sent past Pincer's table (5.2+); a
model the table doesn't know must be sent without any reasoning_effort.

These are real, paid API calls. --dry-run prints every request first (model,
max_tokens, the effort and thinking sent) and sends nothing; a run above
MAX_LIVE_REQUESTS is refused.

Usage:
    # what a run would send, without sending it
    uv run python scripts/smoke_llm.py --dry-run

    # the models configured in .env (default, summary, voice turn model, and failover providers)
    uv run python scripts/smoke_llm.py

    # specific models, as provider:model; one check
    uv run python scripts/smoke_llm.py --model anthropic:claude-haiku-5-5 --model openai:gpt-5-mini
    uv run python scripts/smoke_llm.py --check signature-tamper --switch-model azure:claude-sonnet-5-5

    # the OpenAI-wire checks, on the OpenAI-compatible slot
    uv run python scripts/smoke_llm.py --check openai-effort-low --openai-model azure-openai:gpt-5.4-mini

Prints one PASS / FAIL / SKIP line per check, never a key, a base URL or any
thinking. Exit 0 when nothing failed, 1 otherwise, 2 when refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pincer.config import Settings
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import (
    STOP_REASON_MAX_TOKENS,
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    ToolCall,
)
from pincer.llm.capabilities import endpoint_capabilities
from pincer.llm.model_params import DropReason, ReasoningParams, ReasoningWire, resolve_reasoning, thinking_is_bound
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.llm.replay import wire_tag
from pincer.llm.router import (
    is_openai_wire,
    provider_default_model,
    summary_target,
    voice_turn_target,
    wire_safe_model,
)

if TYPE_CHECKING:
    from openai.types.shared import ReasoningEffort as OpenAIReasoningEffort

    from pincer.config.llm import ReasoningEffort, ThinkingMode

# Matches the voice default (PINCER_VOICE_MAX_RESPONSE_TOKENS=1024): a model that
# spends it before answering fails here as on a call. A ceiling, not a cost.
MAX_TOKENS = 1024
# Every voice check runs at the voice default limit, except the forced cut-off.
VOICE_MAX_TOKENS = 1024
CUTOFF_MAX_TOKENS = 32
# Shortest configured key that `redact` hides; real keys are far longer.
_MIN_REDACTED_KEY_LENGTH = 8
# Cost bound: a run that would send more requests than this is refused (exit 2), nothing sent.
MAX_LIVE_REQUESTS = 40
# The OpenAI-wire checks: small, as reasoning and text share it (max_completion_tokens).
OPENAI_MAX_TOKENS = 512

Status = Literal["PASS", "FAIL", "SKIP"]
BuildFn = Callable[[Settings, str, str], BaseLLMProvider]
# A 400 proves a check only when the same request shape, untouched, was accepted.
CONTROLS = {"signature-tamper": "tool-loop-thinking", "edited-history": "tool-loop-thinking"}
# Invites thinking before the tool call: the checks that send thinking back need some.
LOOP_PROMPT = "Kyiv or Lviv: think about which city's weather matters for a trip today, then call get_weather for Kyiv."
CUTOFF_PROMPT = "Write 300 words about the history of Kyiv's weather, then call get_weather for Kyiv."

WEATHER_TOOL = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

# The cache check's tools: Pincer's only cache breakpoint is on the last tool, so the tools
# block is the cached prefix. ~1,500 tokens, safely above Haiku 5.5's 512-token minimum
# cacheable prompt (down from 4,096 on Haiku 4.5), at a few thousand input tokens per run.
_CITY_FACTS = " ".join(
    f"City {i}: population band {i % 7}, rainfall band {i % 5}, typical spring high {10 + i % 15}C, "
    f"winter low {-12 + i % 9}C, river or coast: {'yes' if i % 2 else 'no'}."
    for i in range(1, 51)
)
CACHE_TOOLS = [
    *WEATHER_TOOL,
    {
        "name": "lookup_city_facts",
        "description": f"Look up stored facts about a city. Reference table: {_CITY_FACTS}",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
]
# Rough size of that prefix (chars / 4), printed by --dry-run.
CACHE_PREFIX_TOKENS = len(json.dumps(CACHE_TOOLS)) // 4


def _provider_model(settings: Settings, name: str) -> str:
    """The model a provider is built with: its own `*_model`, else `default_model`."""
    if name == "anthropic":
        own = settings.anthropic_model
    elif name == "openai":
        own = settings.openai_model
    elif name == settings.openai_compatible_provider:
        own = settings.openai_compatible_model
    elif name == settings.anthropic_compatible_provider:
        own = settings.anthropic_compatible_model
    else:
        own = ""
    return str(own or settings.default_model)


def default_targets(settings: Settings) -> list[tuple[str, str]]:
    """(provider, model) pairs the app would call with this configuration."""
    keys = {
        "anthropic": settings.anthropic_api_key.get_secret_value(),
        "openai": settings.openai_api_key.get_secret_value(),
    }
    known = {*keys, settings.openai_compatible_provider, settings.anthropic_compatible_provider}
    targets: list[tuple[str, str]] = []

    def add(provider: str, model: str) -> None:
        # A well-known provider without a key cannot be built; leave it out
        # rather than report a setup failure for something the app never calls.
        if not provider or not model or keys.get(provider, "set") == "":
            return
        if (provider, model) not in targets:
            targets.append((provider, model))

    # The router's pool: the primary, then the failovers.
    for name in [settings.default_provider, *settings.fallback_providers]:
        add(name, _provider_model(settings, name))
    # Built on demand (voice turn model) when they have a key and their own model.
    if settings.anthropic_model:
        add("anthropic", settings.anthropic_model)
    if settings.openai_model:
        add("openai", settings.openai_model)
    # Same resolution as LLMRouter.get_summarizer().
    summary_provider, summary_model = summary_target(settings)
    if summary_model.startswith("claude") and is_openai_wire(settings, summary_provider):
        summary_model = ""
    add(summary_provider, summary_model or _provider_model(settings, summary_provider))
    # "provider:model" names both; a bare model runs on the default provider.
    turn_provider, sep, turn_model = settings.voice_turn_model.partition(":")
    if sep:
        # An unknown provider name falls back to the default provider and model.
        if turn_provider.strip() in known:
            add(turn_provider.strip(), turn_model.strip())
    else:
        add(settings.default_provider, settings.voice_turn_model.strip())
    return targets


def make_provider(settings: Settings, provider: str, model: str) -> BaseLLMProvider:
    """Build the provider with `model` as its own model, so a model passed on
    the command line works even when that provider has none configured."""
    if provider == "anthropic":
        return AnthropicCompatibleProvider(settings.model_copy(update={"anthropic_model": model}), provider)
    if provider == settings.anthropic_compatible_provider:
        return AnthropicCompatibleProvider(settings.model_copy(update={"anthropic_compatible_model": model}), provider)
    field = "openai_model" if provider == "openai" else "openai_compatible_model"
    return OpenAICompatibleProvider(settings.model_copy(update={field: model}), provider)


async def check_complete(provider: BaseLLMProvider, model: str) -> str:
    messages = [LLMMessage(role=MessageRole.USER, content="Reply with the single word: pong")]
    response = await provider.complete(messages, model=model, temperature=0, max_tokens=MAX_TOKENS)
    if not response.content.strip():
        raise AssertionError(f"empty reply (stop_reason={response.stop_reason}, out={response.output_tokens})")
    return f"{response.content.strip()[:40]!r} ({response.output_tokens} tokens out)"


async def check_stream(provider: BaseLLMProvider, model: str) -> str:
    messages = [LLMMessage(role=MessageRole.USER, content="Reply with the single word: pong")]
    chunks = [
        e.text
        async for e in provider.stream_turn(messages, model=model, temperature=0, max_tokens=MAX_TOKENS)
        if e.text
    ]
    text = "".join(chunks).strip()
    if not text:
        raise AssertionError("stream produced no text")
    return f"{text[:40]!r} ({len(chunks)} chunks)"


async def check_tool_round_trip(provider: BaseLLMProvider, model: str) -> str:
    messages = [LLMMessage(role=MessageRole.USER, content="What's the weather in Kyiv? Use the tool.")]
    first = await provider.complete(messages, tools=WEATHER_TOOL, model=model, temperature=0, max_tokens=MAX_TOKENS)
    if not first.tool_calls:
        raise AssertionError(f"no tool call (stop_reason={first.stop_reason}, content={first.content[:60]!r})")

    messages.append(LLMMessage(role=MessageRole.ASSISTANT, content=first.content, tool_calls=first.tool_calls))
    for call in first.tool_calls:
        messages.append(LLMMessage(role=MessageRole.TOOL_RESULT, content="12C, light rain", tool_call_id=call.id))

    final = None
    async for event in provider.stream_turn(
        messages, tools=WEATHER_TOOL, model=model, temperature=0, max_tokens=MAX_TOKENS
    ):
        if event.response is not None:
            final = event.response
    if final is None or not final.content.strip():
        stop = final.stop_reason if final else "no final event"
        raise AssertionError(f"no answer after the tool result ({stop})")
    return f"{first.tool_calls[0].name}({first.tool_calls[0].arguments}) -> {final.content.strip()[:50]!r}"


CHECKS = (
    ("complete", check_complete),
    ("stream", check_stream),
    ("tool round-trip", check_tool_round_trip),
)


# ── Voice-target checks (issue #345) ─────────────────────


@dataclass(frozen=True)
class Outcome:
    status: Status
    detail: str


@dataclass
class VoiceTarget:
    """What the voice checks run on: the configured voice turn model."""

    name: str
    model: str
    provider: BaseLLMProvider
    switch: tuple[str, str, BaseLLMProvider] | None = None  # --switch-model, built


@dataclass(frozen=True)
class PlannedRequest:
    """One request a run would send, as `--dry-run` prints it (effort and thinking as sent)."""

    target: str
    check: str
    model: str
    max_tokens: int
    effort: str
    thinking: str
    replays_thinking: bool


# The check a request belongs to, for the dry-run list
_current_check: ContextVar[str] = ContextVar("smoke_check", default="")


class DryRunProvider(BaseLLMProvider):  # type: ignore[misc]  # plain mypy can't resolve pincer from scripts/
    """Sends nothing: records each request and answers with a signed thinking block, a tool
    call (when tools are offered) and text, so every check walks its whole path."""

    def __init__(self, settings: Settings, name: str, planned: list[PlannedRequest]) -> None:
        self._settings = settings
        self._name = name
        self._planned = planned
        self._wire = ReasoningWire.OPENAI if is_openai_wire(settings, name) else ReasoningWire.ANTHROPIC

    def _record(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        effort: ReasoningEffort | None,
        thinking: ThinkingMode | None,
    ) -> LLMResponse:
        model = model or provider_default_model(self._settings, self._name)
        max_tokens = max_tokens or self._settings.max_tokens
        params = resolve_reasoning(
            self._wire,
            model,
            self._settings.reasoning_effort if effort is None else effort,
            self._settings.thinking if thinking is None else thinking,
        )
        sent_effort = params.fragment.get("output_config", {}).get("effort") or params.fragment.get("reasoning_effort")
        sent_thinking = params.fragment.get("thinking", {}).get("type", "-")
        if (
            self._wire is ReasoningWire.ANTHROPIC
            and endpoint_capabilities(self._settings, self._name).drop_block
            and thinking_is_bound(model, params)
        ):
            sent_thinking = "adaptive"  # the binding controls ride on adaptive thinking (issue #358)
        self._add(
            model,
            max_tokens,
            _with_dropped(sent_effort or "-", params, "reasoning_effort"),
            _with_dropped(sent_thinking, params, "thinking"),
            any(_carries_thinking(m) for m in messages),
        )
        return LLMResponse(
            content="pong",
            tool_calls=[ToolCall(id="dry_1", name="get_weather", arguments={"city": "Kyiv"})] if tools else [],
            model=model,
            provider=self._name,
            stop_reason=STOP_REASON_MAX_TOKENS if max_tokens <= CUTOFF_MAX_TOKENS else "end_turn",
            raw=[{"type": "thinking", "thinking": "", "signature": "ZHJ5LXJ1bg=="}],
            wire=wire_tag(self._name, model),
        )

    def _add(self, model: str, max_tokens: int, effort: str, thinking: str, replays: bool) -> None:
        self._planned.append(
            PlannedRequest(f"{self._name}:{model}", _current_check.get(), model, max_tokens, effort, thinking, replays)
        )

    def record_raw(self, model: str, effort: OpenAIReasoningEffort) -> None:
        """A request sent past the resolver (`_send_raw_effort`)."""
        self._add(model, OPENAI_MAX_TOKENS, effort or "-", "-", False)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
    ) -> LLMResponse:
        return self._record(messages, tools, model, max_tokens, reasoning_effort, thinking)

    async def close(self) -> None:
        return None


def _with_dropped(sent: str, params: ReasoningParams, setting: str) -> str:
    """What went out for `setting`, or why nothing did: e.g. `low`, `-`, `dropped(unrecognised model)`."""
    dropped = [str(d.reason) for d in params.dropped if d.setting == setting]
    return f"dropped({'; '.join(dropped)})" if dropped else sent


def voice_target(settings: Settings) -> tuple[str, str]:
    """(provider, model) a voice turn asks for, resolved without building anything, so a
    provider that can't be built fails here instead of silently falling back to the default."""
    name, model = voice_turn_target(settings)
    return name, wire_safe_model(settings, name, model) or provider_default_model(settings, name)


async def _turn(
    provider: BaseLLMProvider,
    model: str,
    messages: list[LLMMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = VOICE_MAX_TOKENS,
    effort: ReasoningEffort = "",
    thinking: ThinkingMode = "",
) -> LLMResponse:
    """One voice-style request (`stream_turn`), with effort and thinking given explicitly
    ("" sends nothing) so the owner's global settings never change what is checked."""
    final = None
    async for event in provider.stream_turn(
        messages, tools=tools, model=model, max_tokens=max_tokens, reasoning_effort=effort, thinking=thinking
    ):
        if event.response is not None:
            final = event.response
    if final is None:
        raise AssertionError("no final event")
    return final


def _pong() -> list[LLMMessage]:
    return [LLMMessage(role=MessageRole.USER, content="Reply with the single word: pong")]


def _not_sent(params: ReasoningParams) -> str:
    return ", ".join(f"{d.setting}={d.value} dropped ({d.reason})" for d in params.dropped) or "nothing to send"


async def check_effort_low(t: VoiceTarget) -> Outcome:
    params = resolve_reasoning(ReasoningWire.ANTHROPIC, t.model, "low", "")
    if "output_config" not in params.fragment:
        return Outcome("FAIL", f"effort low is not sent to {t.model}: {_not_sent(params)}")
    reply = await _turn(t.provider, t.model, _pong(), effort="low")
    if not reply.content.strip():
        return Outcome("FAIL", f"empty reply (stop_reason={reply.stop_reason}, out={reply.output_tokens})")
    return Outcome("PASS", f"{reply.content.strip()[:40]!r} ({reply.output_tokens} tokens out)")


def _carries_thinking(message: LLMMessage) -> bool:
    return any(block["type"] in ("thinking", "redacted_thinking") for block in message.raw)


def _thinking_blocks(reply: LLMResponse) -> list[dict[str, Any]]:
    """The thinking blocks of a reply's raw output, in order."""
    return [b for b in reply.raw if b["type"] in ("thinking", "redacted_thinking")]


def _thinking_summary(blocks: list[dict[str, Any]]) -> str:
    """How many thinking blocks came back and whether each is signed — never their content."""
    marks = [
        "redacted" if b["type"] == "redacted_thinking" else "signed" if b.get("signature") else "UNSIGNED"
        for b in blocks
    ]
    return f"{len(blocks)} thinking block(s) returned ({', '.join(marks) or 'none'})"


async def check_thinking_disabled(t: VoiceTarget) -> Outcome:
    params = resolve_reasoning(ReasoningWire.ANTHROPIC, t.model, "", "disabled")
    if any(d.setting == "thinking" for d in params.dropped):
        # Unlike a dropped effort (FAIL): some models can't turn thinking off at all (Opus 5.5)
        return Outcome("SKIP", f"disabled not supported by {t.model}; nothing to check")
    if "thinking" not in params.fragment:
        return Outcome("SKIP", f"{t.model} does not think by default; nothing to check")
    reply = await _turn(t.provider, t.model, _pong(), thinking="disabled")
    if _thinking_blocks(reply):
        return Outcome("FAIL", _thinking_summary(_thinking_blocks(reply)))
    if not reply.content.strip():
        return Outcome("FAIL", f"empty reply (stop_reason={reply.stop_reason}, out={reply.output_tokens})")
    return Outcome("PASS", f"{reply.content.strip()[:40]!r}, no thinking returned")


async def _tool_step(
    t: VoiceTarget, tools: list[dict[str, Any]] = WEATHER_TOOL
) -> tuple[list[LLMMessage], LLMResponse]:
    """Step 1 of a tool loop, as the voice turn sends it (no effort or thinking set: the
    model's default), and the history step 2 sends: the response with its thinking, then
    the tool results. Raises when there is no tool call or no thinking to send back."""
    messages = [LLMMessage(role=MessageRole.USER, content=LOOP_PROMPT)]
    first = await _turn(t.provider, t.model, messages, tools=tools)
    if not first.tool_calls:
        raise AssertionError(f"no tool call (stop_reason={first.stop_reason})")
    if not _thinking_blocks(first):
        raise AssertionError(f"no thinking returned; replay unproven (stop_reason={first.stop_reason})")
    messages.append(LLMMessage.from_response(first))
    for call in first.tool_calls:
        messages.append(LLMMessage(role=MessageRole.TOOL_RESULT, content="12C, light rain", tool_call_id=call.id))
    return messages, first


def _answered(reply: LLMResponse) -> str:
    """The reply's text for a detail line; raises when there is none."""
    if not reply.content.strip():
        raise AssertionError(f"no answer after the tool result (stop_reason={reply.stop_reason})")
    return repr(reply.content.strip()[:50])


async def check_tool_loop_thinking(t: VoiceTarget) -> Outcome:
    messages, first = await _tool_step(t)
    final = await _turn(t.provider, t.model, messages, tools=WEATHER_TOOL)
    summary = _thinking_summary(_thinking_blocks(first))
    return Outcome("PASS", f"{summary}, sent back and accepted -> {_answered(final)}")


def http_status(exc: BaseException) -> int | None:
    """The HTTP status behind a provider error (`LLMError` wraps the SDK's), if any."""
    seen: BaseException | None = exc
    while seen is not None:
        status = getattr(seen, "status_code", None)
        if isinstance(status, int):
            return status
        seen = seen.__cause__
    return None


async def _expect_400(t: VoiceTarget, messages: list[LLMMessage], what: str) -> Outcome:
    """Step 2 of a tool loop that the API must refuse with a 400."""
    try:
        await _turn(t.provider, t.model, messages, tools=WEATHER_TOOL)
    except Exception as e:
        status = http_status(e)
        if status == 400:
            return Outcome("PASS", f"{what} rejected with 400: {e}")
        return Outcome("FAIL", f"{what}: expected a 400, got {status or type(e).__name__}: {e}")
    return Outcome("FAIL", f"{what} was accepted; expected a 400")


def _tampered(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`raw` with one character in the middle of the first thinking signature changed (still base64)."""
    out = deepcopy(raw)
    for block in out:
        signature = block.get("signature")
        if block["type"] == "thinking" and signature:
            mid = len(signature) // 2
            swap = "B" if signature[mid] == "A" else "A"
            block["signature"] = signature[:mid] + swap + signature[mid + 1 :]
            return out
    raise AssertionError("no signed thinking block to tamper with")


async def check_signature_tamper(t: VoiceTarget) -> Outcome:
    messages, first = await _tool_step(t)
    messages[1].raw = _tampered(first.raw)
    return await _expect_400(t, messages, "tampered signature")


async def check_edited_history(t: VoiceTarget) -> Outcome:
    """Q2c: a thinking block is valid only while everything before it is unchanged."""
    messages, _ = await _tool_step(t)
    messages[0] = LLMMessage(role=MessageRole.USER, content=messages[0].content + " Keep it short.")
    return await _expect_400(t, messages, "edited history")


async def check_changed_tools_continuation(t: VoiceTarget) -> Outcome:
    """A skill load or a language change mid-turn changes the tools or the system prompt; production
    sends the turn's thinking on anyway and the API drops what no longer fits (issue #358). Step 2
    here carries another tool list: it must be accepted, and the blocks the API reports dropping are
    counted (the thinking the next request lost, none of it is shown)."""
    messages, first = await _tool_step(t)
    final = await _turn(t.provider, t.model, messages, tools=CACHE_TOOLS)
    summary = _thinking_summary(_thinking_blocks(first))
    dropped = f"{len(final.thinking_dropped)} block(s) dropped by the API"
    return Outcome("PASS", f"{summary}, tools changed, accepted ({dropped}) -> {_answered(final)}")


async def check_model_switch(t: VoiceTarget) -> Outcome:
    """Q3c: thinking sent unchanged to another model; the API drops what the new model can't read.
    Pincer itself never does this (a message from another wire is rebuilt without its thinking, D9):
    the history's wire is set to the switch model's here, to probe the API's own behaviour."""
    if t.switch is None:
        return Outcome("SKIP", "no --switch-model given")
    switch_name, switch_model, switch_provider = t.switch
    messages, first = await _tool_step(t)
    for message in messages:
        if message.raw:
            message.wire = wire_tag(switch_name, switch_model)
    final = await _turn(switch_provider, switch_model, messages, tools=WEATHER_TOOL)
    summary = _thinking_summary(_thinking_blocks(first))
    return Outcome("PASS", f"{summary}, sent unchanged to {switch_model}, accepted -> {_answered(final)}")


async def check_cache_reads(t: VoiceTarget) -> Outcome:
    """Q2b: prompt-cache reads on two voice-style turns, the second sent with the first's thinking kept
    (production never strips it, issue #358). Recorded, never judged: 0 is a finding."""
    messages, first = await _tool_step(t, CACHE_TOOLS)
    answer = await _turn(t.provider, t.model, messages, tools=CACHE_TOOLS)
    messages.append(LLMMessage.from_response(answer))
    messages.append(LLMMessage(role=MessageRole.USER, content="And Lviv? Answer from what you know, no tool."))
    kept = await _turn(t.provider, t.model, messages, tools=CACHE_TOOLS)
    return Outcome(
        "PASS",
        f"cache_read_tokens turn 1: {first.cache_read_tokens}, {answer.cache_read_tokens}; "
        f"turn 2: {kept.cache_read_tokens} "
        f"(tools prefix ~{CACHE_PREFIX_TOKENS} tokens; reads also cover the conversation)",
    )


async def check_forced_cutoff(t: VoiceTarget) -> Outcome:
    """Slices 1-2 recognise a cut-off by this exact stop_reason; confirm the endpoint sends it."""
    messages = [LLMMessage(role=MessageRole.USER, content=CUTOFF_PROMPT)]
    reply = await _turn(t.provider, t.model, messages, tools=WEATHER_TOOL, max_tokens=CUTOFF_MAX_TOKENS)
    if reply.stop_reason != STOP_REASON_MAX_TOKENS:
        return Outcome("FAIL", f"stop_reason={reply.stop_reason!r}, expected {STOP_REASON_MAX_TOKENS!r}")
    return Outcome("PASS", f"stop_reason={reply.stop_reason!r} ({reply.output_tokens} tokens out)")


VOICE_CHECKS: tuple[tuple[str, Callable[[VoiceTarget], Awaitable[Outcome]]], ...] = (
    ("effort-low", check_effort_low),
    ("thinking-disabled", check_thinking_disabled),
    ("tool-loop-thinking", check_tool_loop_thinking),
    ("signature-tamper", check_signature_tamper),
    ("edited-history", check_edited_history),
    ("changed-tools-continuation", check_changed_tools_continuation),
    ("model-switch", check_model_switch),
    ("cache-reads", check_cache_reads),
    ("forced-cutoff", check_forced_cutoff),
)


# ── OpenAI-wire reasoning checks (slice 8's table, against the live endpoint) ──


def _openai_params(model: str, effort: ReasoningEffort, thinking: ThinkingMode) -> ReasoningParams:
    return resolve_reasoning(ReasoningWire.OPENAI, model, effort, thinking)


def _openai_recognised(model: str) -> bool:
    return not any(d.reason is DropReason.UNRECOGNISED_MODEL for d in _openai_params(model, "low", "").dropped)


def _openai_unrecognised_skip(model: str) -> Outcome | None:
    if _openai_recognised(model):
        return None
    return Outcome("SKIP", f"{model} is not recognised; see openai-unrecognised")


async def _openai_pong(
    provider: BaseLLMProvider, model: str, effort: ReasoningEffort, thinking: ThinkingMode
) -> LLMResponse:
    return await provider.complete(
        _pong(), model=model, max_tokens=OPENAI_MAX_TOKENS, reasoning_effort=effort, thinking=thinking
    )


def _text(reply: LLMResponse) -> str:
    """The reply's text for a detail line; raises when there is none."""
    if not reply.content.strip():
        raise AssertionError(f"empty reply (stop_reason={reply.stop_reason}, out={reply.output_tokens})")
    return repr(reply.content.strip()[:40])


async def check_openai_effort_low(provider: BaseLLMProvider, model: str) -> Outcome:
    if (skip := _openai_unrecognised_skip(model)) is not None:
        return skip
    params = _openai_params(model, "low", "")
    if params.fragment.get("reasoning_effort") != "low":
        return Outcome("FAIL", f"effort low is not sent to {model}: {_not_sent(params)}")
    reply = await _openai_pong(provider, model, "low", "")
    return Outcome("PASS", f"reasoning_effort=low accepted: {_text(reply)}")


async def check_openai_disabled(provider: BaseLLMProvider, model: str) -> Outcome:
    """`disabled` goes out as the row's off level (none from gpt-5.1 on)."""
    if (skip := _openai_unrecognised_skip(model)) is not None:
        return skip
    params = _openai_params(model, "", "disabled")
    sent = params.fragment.get("reasoning_effort")
    if sent is None:
        return Outcome("SKIP", f"disabled not supported by {model}; nothing to check")
    reply = await _openai_pong(provider, model, "", "disabled")
    return Outcome("PASS", f"reasoning_effort={sent} accepted: {_text(reply)}")


async def check_openai_xhigh(provider: BaseLLMProvider, model: str) -> Outcome:
    """Judged on the 200 alone: at xhigh the small limit may go to reasoning before any text."""
    if (skip := _openai_unrecognised_skip(model)) is not None:
        return skip
    sent = _openai_params(model, "xhigh", "").fragment.get("reasoning_effort")
    if sent != "xhigh":
        return Outcome("SKIP", f"{model} takes no xhigh (the resolver sends {sent or 'nothing'})")
    reply = await _openai_pong(provider, model, "xhigh", "")
    return Outcome(
        "PASS", f"reasoning_effort=xhigh accepted (stop_reason={reply.stop_reason}, out={reply.output_tokens})"
    )


async def _send_raw_effort(provider: BaseLLMProvider, model: str, effort: OpenAIReasoningEffort) -> None:
    """A request carrying a reasoning_effort Pincer's table never sends, past the resolver."""
    if isinstance(provider, DryRunProvider):
        provider.record_raw(model, effort)
        return
    if not isinstance(provider, OpenAICompatibleProvider):
        raise AssertionError(f"not an OpenAI-wire provider: {type(provider).__name__}")
    await provider._client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: pong"}],
        max_completion_tokens=OPENAI_MAX_TOKENS,
        reasoning_effort=effort,
    )


async def check_openai_minimal(provider: BaseLLMProvider, model: str) -> Outcome:
    """Informational: does gpt-5.2+ take `minimal`? Public sources disagree; the table never sends it."""
    if (skip := _openai_unrecognised_skip(model)) is not None:
        return skip
    # The gpt-5.2+ rows are the ones that take xhigh
    if _openai_params(model, "xhigh", "").fragment.get("reasoning_effort") != "xhigh":
        return Outcome("SKIP", "gpt-5.2 and later only")
    try:
        await _send_raw_effort(provider, model, "minimal")
    except Exception as e:
        status = http_status(e)
        if status == 400:
            return Outcome("PASS", f"rejected with 400: the table is right (Pincer never sends it): {e}")
        return Outcome("FAIL", f"expected a 200 or a 400, got {status or type(e).__name__}: {e}")
    return Outcome("PASS", "accepted: the table is conservative (Pincer sends none)")


async def check_openai_unrecognised(provider: BaseLLMProvider, model: str) -> Outcome:
    """A model the resolver doesn't know gets no reasoning_effort at all, and still answers."""
    if _openai_recognised(model):
        return Outcome("SKIP", f"{model} is recognised")
    params = _openai_params(model, "low", "")
    if params.fragment:
        return Outcome("FAIL", f"something is still sent to {model}: {params.fragment}")
    reply = await _openai_pong(provider, model, "low", "")
    return Outcome("PASS", f"effort low dropped (unrecognised model), sent without it: {_text(reply)}")


OPENAI_CHECKS: tuple[tuple[str, Callable[[BaseLLMProvider, str], Awaitable[Outcome]]], ...] = (
    ("openai-effort-low", check_openai_effort_low),
    ("openai-disabled", check_openai_disabled),
    ("openai-xhigh", check_openai_xhigh),
    ("openai-minimal", check_openai_minimal),
    ("openai-unrecognised", check_openai_unrecognised),
)
CHECK_NAMES = (*(label for label, _ in CHECKS), *(name for name, _ in VOICE_CHECKS), *(n for n, _ in OPENAI_CHECKS))


async def run(
    settings: Settings,
    targets: list[tuple[str, str]],
    *,
    voice: tuple[str, str] | None = None,
    switch: tuple[str, str] | None = None,
    openai: list[tuple[str, str]] | None = None,
    only: frozenset[str] = frozenset(),
    build: BuildFn = make_provider,
    emit: Callable[[str], object] = print,
) -> int:
    """Run the selected checks (all when `only` is empty); the number that failed.
    Every line goes out through `redact`."""
    openai = openai or []
    failures = 0
    passed: set[str] = set()

    def report(label: str, outcome: Outcome) -> None:
        nonlocal failures
        control = CONTROLS.get(label)
        if outcome.status == "PASS" and control is not None and control not in passed:
            outcome = Outcome("FAIL", f"inconclusive: control ({control}) did not pass; {outcome.detail}")
        if outcome.status == "PASS":
            passed.add(label)
        emit(redact(f"  {outcome.status:<4}  {label}: {outcome.detail}", settings))
        failures += outcome.status == "FAIL"

    async def attempt(label: str, check: Awaitable[Outcome]) -> None:
        _current_check.set(label)
        try:
            outcome = await check
        except Exception as e:  # each check reports on its own; one 400 must not hide the rest
            outcome = Outcome("FAIL", str(e) or type(e).__name__)
        report(label, outcome)

    async def basic_check(
        check: Callable[[BaseLLMProvider, str], Awaitable[str]], provider: BaseLLMProvider, model: str
    ) -> Outcome:
        return Outcome("PASS", await check(provider, model))  # these raise on failure

    basic = [(label, check) for label, check in CHECKS if not only or label in only]
    for provider_name, model in targets if basic else []:
        emit(f"{provider_name}:{model}")
        try:
            provider = build(settings, provider_name, model)
        except ValueError as e:
            report("provider setup", Outcome("FAIL", str(e)))
            continue
        for label, check in basic:
            await attempt(label, basic_check(check, provider, model))
        await provider.close()

    wanted = set(only)
    wanted |= {CONTROLS[name] for name in only if name in CONTROLS}
    voice_checks = [(name, check) for name, check in VOICE_CHECKS if not only or name in wanted]
    if voice_checks and voice is not None:
        name, model = voice
        emit(f"voice {name}:{model}")
        if is_openai_wire(settings, name):
            for label, _ in voice_checks:
                report(label, Outcome("SKIP", f"{name} is OpenAI-wire; the voice checks are Anthropic-wire only"))
        else:
            try:
                target = VoiceTarget(name, model, build(settings, name, model))
                if switch is not None and any(label == "model-switch" for label, _ in voice_checks):
                    target.switch = (*switch, build(settings, *switch))
            except ValueError as e:
                report("provider setup", Outcome("FAIL", str(e)))
            else:
                for label, voice_check in voice_checks:
                    await attempt(label, voice_check(target))
                await target.provider.close()
                if target.switch is not None:
                    await target.switch[2].close()

    openai_checks = [(name, check) for name, check in OPENAI_CHECKS if not only or name in only]
    if openai_checks and not openai:
        for label, _ in openai_checks:
            report(label, Outcome("SKIP", "no --openai-model given"))
    for provider_name, model in openai if openai_checks else []:
        emit(f"openai {provider_name}:{model}")
        try:
            provider = build(settings, provider_name, model)
        except ValueError as e:
            report("provider setup", Outcome("FAIL", str(e)))
            continue
        for label, openai_check in openai_checks:
            await attempt(label, openai_check(provider, model))
        await provider.close()
    return failures


def voice_collision(settings: Settings, name: str) -> str | None:
    """Why the voice target's provider name can't be trusted, if it can't: a name both
    compatible slots use resolves to the OpenAI one (follow-up #12)."""
    if name and name == settings.openai_compatible_provider == settings.anthropic_compatible_provider:
        return (
            f"provider name {name!r} is used by both compatible slots; "
            "the voice target would resolve to the OpenAI slot"
        )
    return None


def redact(text: str, settings: Settings) -> str:
    """`text` without any configured API key or base URL (error messages can carry them)."""
    keys = [
        settings.anthropic_api_key.get_secret_value(),
        settings.anthropic_compatible_api_key.get_secret_value(),
        settings.openai_api_key.get_secret_value(),
        settings.openai_compatible_api_key.get_secret_value(),
    ]
    urls = [settings.anthropic_compatible_base_url, settings.openai_compatible_base_url, settings.openai_base_url]
    # A key shorter than that is a placeholder ("x", "none"): replacing it would garble every line
    secrets = {k for k in keys if len(k) >= _MIN_REDACTED_KEY_LENGTH} | {u for u in urls if u}
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


async def plan_requests(
    settings: Settings,
    targets: list[tuple[str, str]],
    *,
    voice: tuple[str, str] | None = None,
    switch: tuple[str, str] | None = None,
    openai: list[tuple[str, str]] | None = None,
    only: frozenset[str] = frozenset(),
) -> list[PlannedRequest]:
    """Every request `run` would send with these arguments, found by running the same checks
    against `DryRunProvider`s: nothing is built for real and nothing is sent."""
    planned: list[PlannedRequest] = []
    await run(
        settings,
        targets,
        voice=voice,
        switch=switch,
        openai=openai,
        only=only,
        build=lambda s, name, _model: DryRunProvider(s, name, planned),
        emit=lambda _line: None,
    )
    return planned


def _plan_line(request: PlannedRequest) -> str:
    return (
        f"{request.target} check={request.check} max_tokens={request.max_tokens} "
        f"effort={request.effort} thinking={request.thinking} replay={'yes' if request.replays_thinking else 'no'}"
    )


def _target_spec(ap: argparse.ArgumentParser, flag: str, spec: str) -> tuple[str, str]:
    provider_name, sep, model = spec.partition(":")
    if not sep or not provider_name or not model:
        ap.error(f"{flag} wants PROVIDER:MODEL, got {spec!r}")
    return provider_name, model


def main(argv: list[str] | None = None, *, settings: Settings | None = None, build: BuildFn = make_provider) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="PROVIDER:MODEL",
        help="model for the basic checks, repeatable (default: the models configured in .env)",
    )
    ap.add_argument(
        "--voice-model",
        metavar="PROVIDER:MODEL",
        help="model for the voice checks (default: the configured voice turn model)",
    )
    ap.add_argument(
        "--switch-model",
        metavar="PROVIDER:MODEL",
        help="a second model on the same deployment for the model-switch check (skipped without it)",
    )
    ap.add_argument(
        "--openai-model",
        action="append",
        default=[],
        metavar="PROVIDER:MODEL",
        help="OpenAI-wire model for the reasoning_effort checks, repeatable (skipped without it)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print every request a run would send, and send nothing")
    ap.add_argument(
        "--check",
        action="append",
        default=[],
        choices=CHECK_NAMES,
        metavar="NAME",
        help=f"run only this check, repeatable: {', '.join(CHECK_NAMES)}",
    )
    args = ap.parse_args(argv)

    settings = settings or Settings()  # type: ignore[call-arg]
    targets = [_target_spec(ap, "--model", spec) for spec in args.model] or default_targets(settings)
    voice: tuple[str, str] | None = (
        _target_spec(ap, "--voice-model", args.voice_model) if args.voice_model else voice_target(settings)
    )
    switch = _target_spec(ap, "--switch-model", args.switch_model) if args.switch_model else None
    openai = [_target_spec(ap, "--openai-model", spec) for spec in args.openai_model]

    if not targets and not args.voice_model:
        print("Nothing to test: no provider key configured, or pass --model PROVIDER:MODEL", file=sys.stderr)
        return 1

    only = frozenset(args.check)
    setup_failures = 0
    collision = voice_collision(settings, voice[0]) if voice else None
    if collision is not None:
        print(redact(f"  FAIL  voice target: {collision}", settings))
        voice, setup_failures = None, 1
    planned = asyncio.run(plan_requests(settings, targets, voice=voice, switch=switch, openai=openai, only=only))
    footer = (
        f"{len(planned)} requests, largest max_tokens {max((r.max_tokens for r in planned), default=0)}; "
        f"cache check tools prefix ~{CACHE_PREFIX_TOKENS} tokens per request (bound: {MAX_LIVE_REQUESTS} requests)"
    )
    if args.dry_run:
        for request in planned:
            print(redact(_plan_line(request), settings))
        print(f"\n{footer}; nothing was sent")
        return 2 if len(planned) > MAX_LIVE_REQUESTS else min(setup_failures, 1)
    if len(planned) > MAX_LIVE_REQUESTS:
        print(f"Refusing to run: {footer}. Narrow it with --model or --check.", file=sys.stderr)
        return 2

    failures = setup_failures + asyncio.run(
        run(settings, targets, voice=voice, switch=switch, openai=openai, only=only, build=build)
    )
    print(f"\n{failures} check(s) failed" if failures else "\nAll checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
