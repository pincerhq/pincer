"""Reasoning settings per API: gpt-6 reasons with tools on the Responses API (issue #358, slice 8).

The same settings resolve differently for the two OpenAI request formats: Chat Completions takes gpt-6's
function tools only with reasoning off, the Responses API takes any effort with them.
"""

from __future__ import annotations

import pytest
from openai_wire import ScriptedOpenAI, Turn, message_item
from pydantic import SecretStr

from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.capabilities import reasoning_wire
from pincer.llm.model_params import DroppedSetting, DropReason, ReasoningParams, ReasoningWire, resolve_reasoning

CHAT, RESPONSES = ReasoningWire.OPENAI, ReasoningWire.OPENAI_RESPONSES
_WITH_OFF_LEVEL = ["gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna"]
_WITHOUT = ["gpt-6.1-sol", "gpt-6-astra"]


def _fragment(effort: str) -> ReasoningParams:
    return ReasoningParams(fragment={"reasoning_effort": effort})


# ── The endpoint decides the wire ────────────────────────────────────────────


def test_an_endpoints_wire_is_the_api_its_capabilities_say(settings):
    settings = settings.model_copy(
        update={"openai_compatible_provider": "gw", "anthropic_compatible_provider": "foundry"}
    )

    assert reasoning_wire(settings, "openai") is RESPONSES
    assert reasoning_wire(settings, "gw") is CHAT  # compatible endpoints are not on the default list
    assert reasoning_wire(settings, "foundry") is ReasoningWire.ANTHROPIC
    assert reasoning_wire(settings, "anthropic") is ReasoningWire.ANTHROPIC
    assert reasoning_wire(settings, "nobody") is None
    listed = settings.model_copy(update={"responses_api_providers": ["gw"]})
    assert (reasoning_wire(listed, "gw"), reasoning_wire(listed, "openai")) == (RESPONSES, CHAT)


# ── gpt-6 with tools, per wire ───────────────────────────────────────────────


@pytest.mark.parametrize("model", _WITH_OFF_LEVEL + _WITHOUT)
@pytest.mark.parametrize("with_tools", [True, False])
def test_on_responses_nothing_set_sends_nothing_even_with_tools(model, with_tools):
    assert resolve_reasoning(RESPONSES, model, "", "", with_tools=with_tools) == ReasoningParams()


@pytest.mark.parametrize("model", _WITH_OFF_LEVEL)
def test_on_chat_completions_a_tool_turn_with_nothing_set_still_sends_none(model):
    assert resolve_reasoning(CHAT, model, "", "", with_tools=True) == _fragment("none")


@pytest.mark.parametrize("model", _WITH_OFF_LEVEL + _WITHOUT)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("with_tools", [True, False])
def test_on_responses_any_effort_goes_through_with_tools(model, effort, with_tools):
    assert resolve_reasoning(RESPONSES, model, effort, "", with_tools=with_tools) == _fragment(effort)


@pytest.mark.parametrize("model", _WITH_OFF_LEVEL)
def test_on_responses_thinking_disabled_sends_none_over_an_effort(model):
    assert resolve_reasoning(RESPONSES, model, "high", "disabled", with_tools=True) == _fragment("none")


@pytest.mark.parametrize("model", _WITHOUT)
def test_on_responses_a_model_with_no_off_level_drops_thinking_disabled(model):
    result = resolve_reasoning(RESPONSES, model, "low", "disabled", with_tools=True)

    assert result == ReasoningParams(
        fragment={"reasoning_effort": "low"},
        dropped=(DroppedSetting("thinking", "disabled", DropReason.UNSUPPORTED_BY_MODEL),),
    )


# ── The request bodies of a listed endpoint ──────────────────────────────────

_TOOLS = [{"name": "get_weather", "description": "Weather", "input_schema": {"type": "object", "properties": {}}}]


async def _reasoning_sent(settings, model: str, method: str, **call) -> object:
    """The `reasoning` field of the request body ('absent' when there is none) for one call."""
    settings = settings.model_copy(
        update={
            "openai_model": model,
            "openai_api_key": SecretStr("sk-test"),
            "temperature": 0.5,
            **call.pop("overrides", {}),
        }
    )
    reply = Turn(text=["ok"]) if method == "stream_turn" else [message_item("ok")]
    llm = ScriptedOpenAI(settings, [reply], model=model)
    request = [LLMMessage(role=MessageRole.USER, content="hi")]
    if method == "stream_turn":
        [e async for e in llm.provider.stream_turn(request, tools=_TOOLS, **call)]
    else:
        await llm.provider.complete(request, tools=_TOOLS, **call)
    assert llm.paths == ["/v1/responses"]  # precondition: a listed endpoint
    assert llm.requests[0]["tools"]  # precondition: a tool turn
    return llm.requests[0].get("reasoning", "absent")


@pytest.mark.parametrize("method", ["complete", "stream_turn"])
@pytest.mark.parametrize(
    ("model", "settings_", "call", "sent"),
    [
        ("gpt-6.1-terra", {}, {}, "absent"),
        ("gpt-6.1-terra", {"reasoning_effort": "high"}, {}, {"effort": "high"}),
        ("gpt-6.1-terra", {"thinking": "disabled"}, {}, {"effort": "none"}),
        ("gpt-6-astra", {}, {}, "absent"),
        ("gpt-6-astra", {"reasoning_effort": "low"}, {}, {"effort": "low"}),
        ("gpt-6.1-sol", {"reasoning_effort": "max"}, {}, {"effort": "max"}),
        # a voice call's per-call values: "" (the `default` override) sends nothing over a global effort
        ("gpt-6.1-terra", {"reasoning_effort": "high"}, {"reasoning_effort": "", "thinking": ""}, "absent"),
        ("gpt-6.1-terra", {"reasoning_effort": "high"}, {"reasoning_effort": "low"}, {"effort": "low"}),
    ],
    ids=[
        "terra-unset",
        "terra-high",
        "terra-disabled",
        "astra-unset",
        "astra-low",
        "sol-max",
        "voice-default",
        "voice-low",
    ],
)
async def test_a_tool_turn_on_a_listed_endpoint_sends_what_responses_takes(
    settings, method, model, settings_, call, sent
):
    assert await _reasoning_sent(settings, model, method, overrides=settings_, **call) == sent


# ── One place decides the wire ───────────────────────────────────────────────


def test_no_call_site_derives_a_reasoning_wire_of_its_own():
    """`llm.capabilities.reasoning_wire` is the only mapping from an endpoint to a wire. A conditional that
    picks a `ReasoningWire`, or a private copy of the mapping, would silently keep Chat Completions rules for
    an endpoint on the Responses API (the bug this slice removes)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    derivation = re.compile(r"ReasoningWire\.\w+ if |else ReasoningWire\.\w+|def _reasoning_wire|\b_reasoning_wire\(")
    offenders = [
        f"{path.relative_to(root)}:{number}: {line.strip()}"
        for folder in ("src", "scripts")
        for path in sorted((root / folder).rglob("*.py"))
        if path.name not in {"capabilities.py"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if derivation.search(line)
    ]

    assert offenders == []


def test_the_gpt_6_refusal_and_the_changelog_say_what_changed():
    from pathlib import Path

    from pincer.llm.pricing_check import GPT6_TOOLS_EXPLANATION

    assert "#358" not in GPT6_TOOLS_EXPLANATION
    assert "PINCER_RESPONSES_API_PROVIDERS" in GPT6_TOOLS_EXPLANATION
    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    for needed in (
        "gpt-6 on OpenAI and Azure now reasons with tools",
        "medium on OpenAI's documentation",
        "PINCER_THINKING=disabled",
        "PINCER_VOICE_REASONING_EFFORT=low",
        "PINCER_VOICE_MAX_RESPONSE_TOKENS",
        "220-token",
    ):
        assert needed in changelog
