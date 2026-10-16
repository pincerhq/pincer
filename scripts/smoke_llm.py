#!/usr/bin/env python3
"""Answer one question: do the configured LLM models accept the requests we send?

Unit tests mock the SDKs, so they cannot tell when a model starts rejecting a
request param (a 400 on `temperature`, `max_tokens`, …) or stops returning tool
calls. This sends the same three request shapes the app does — `complete`,
`stream`, and a tool round-trip ending in `stream_turn` — through the real
providers and reports each one.

These are real, paid API calls: a handful of tokens per model.

Usage:
    # the models configured in .env (default, summary, voice turn model, and failover providers)
    uv run python scripts/smoke_llm.py

    # specific models, as provider:model
    uv run python scripts/smoke_llm.py --model anthropic:claude-haiku-5-5 --model openai:gpt-5-mini

Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from pincer.config import Settings
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.llm.router import is_openai_wire, summary_target

if TYPE_CHECKING:
    from pincer.llm.base import BaseLLMProvider

# Small on purpose: voice turns run at max_tokens=150, and a model that spends
# its budget before answering fails here the same way it would on a call.
MAX_TOKENS = 150

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
    return own or settings.default_model


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
    chunks = [t async for t in provider.stream(messages, model=model, temperature=0, max_tokens=MAX_TOKENS)]
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


async def run(settings: Settings, targets: list[tuple[str, str]]) -> int:
    failures = 0
    for provider_name, model in targets:
        print(f"{provider_name}:{model}")
        try:
            provider = make_provider(settings, provider_name, model)
        except ValueError as e:
            print(f"  FAIL  provider setup: {e}")
            failures += 1
            continue
        for label, check in CHECKS:
            try:
                detail = await check(provider, model)
            except Exception as e:  # each check reports on its own; one 400 must not hide the rest
                print(f"  FAIL  {label}: {e}")
                failures += 1
            else:
                print(f"  ok    {label}: {detail}")
        await provider.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="PROVIDER:MODEL",
        help="model to test, repeatable (default: the models configured in .env)",
    )
    args = ap.parse_args()

    settings = Settings()
    targets: list[tuple[str, str]] = []
    for spec in args.model:
        provider_name, sep, model = spec.partition(":")
        if not sep or not provider_name or not model:
            ap.error(f"--model wants PROVIDER:MODEL, got {spec!r}")
        targets.append((provider_name, model))
    targets = targets or default_targets(settings)

    if not targets:
        print("Nothing to test: no provider key configured, or pass --model PROVIDER:MODEL", file=sys.stderr)
        return 1

    failures = asyncio.run(run(settings, targets))
    print(f"\n{failures} check(s) failed" if failures else "\nAll checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
