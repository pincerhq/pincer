"""Internal JSON-extraction calls pin effort low and thinking disabled (issue #345, review round 1).

Their output limit is small and fixed, so a model that thinks past it would cut the JSON
off and lose the outcome or summary. The pin holds whatever the global setting is.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, text

from pincer.core.agent import Agent
from pincer.voice.outcome import extract_outcome
from pincer.voice.threads import merge_summary

_JSON_REPLY = [([text('{"summary": "s", "satisfied_commitments": []}')], "end_turn")]


def _global_max_adaptive(settings, model: str):
    settings.anthropic_model = model
    settings.reasoning_effort = "max"
    settings.thinking = "adaptive"
    return settings


async def _outcome_request(settings) -> dict[str, Any]:
    async with ScriptedAnthropic(settings, _JSON_REPLY) as llm:
        await extract_outcome(llm.provider, "AGENT: hi\nCALLER: bye", "")
    return llm.requests[0]


async def _merge_request(settings) -> dict[str, Any]:
    async with ScriptedAnthropic(settings, _JSON_REPLY) as llm:
        await merge_summary(llm.provider, "", {"task_result": "completed"}, [], "en")
    return llm.requests[0]


async def _onboarding_request(settings, session_manager, cost_tracker, tool_registry) -> dict[str, Any]:
    async with ScriptedAnthropic(settings, _JSON_REPLY) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tool_registry)
        await agent._ingest_onboarding_reply("I'm Bob", "user-pin", "web")
    return llm.requests[0]


@pytest.fixture(params=["outcome", "merge", "onboarding"])
def structured_request(request, settings, session_manager, cost_tracker, tool_registry):
    """Each structured call, as a coroutine function taking the model it runs on."""

    async def run(model: str) -> dict[str, Any]:
        _global_max_adaptive(settings, model)
        if request.param == "outcome":
            return await _outcome_request(settings)
        if request.param == "merge":
            return await _merge_request(settings)
        return await _onboarding_request(settings, session_manager, cost_tracker, tool_registry)

    return run


async def test_global_max_effort_and_adaptive_thinking_do_not_reach_a_structured_call(structured_request):
    body = await structured_request("claude-opus-4-7")

    assert body["output_config"] == {"effort": "low"}
    assert "thinking" not in body  # Opus 4.7 does not think unless asked: nothing to disable


async def test_a_structured_call_turns_thinking_off_where_the_model_thinks_by_default(structured_request):
    body = await structured_request("claude-haiku-5-5")

    assert body["output_config"] == {"effort": "low"}
    assert body["thinking"] == {"type": "disabled"}


async def test_a_structured_call_on_a_model_that_cannot_disable_thinking_still_gets_low_effort(structured_request):
    body = await structured_request("claude-opus-5-5")  # thinks always: `disabled` is dropped, low still applies

    assert body["output_config"] == {"effort": "low"}
    assert "thinking" not in body


async def test_summaries_keep_their_own_settings_and_chat_keeps_the_global_ones(settings):
    """The pin is for the extraction calls only: a plain call on the same provider still gets the global values."""
    _global_max_adaptive(settings, "claude-opus-4-7")
    async with ScriptedAnthropic(settings, [([text("ok")], "end_turn")]) as llm:
        from pincer.llm.base import LLMMessage, MessageRole

        await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert llm.requests[0]["output_config"] == {"effort": "max"}
    assert llm.requests[0]["thinking"] == {"type": "adaptive"}


# ── A dropped pin is not the operator's mistake ────────────────────────────────


def _dropped(caplog: pytest.LogCaptureFixture, model: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "Not sending" in r.getMessage() and model in r.getMessage()]


async def test_a_dropped_pin_is_logged_at_debug_when_nothing_is_set_globally(settings, caplog):
    """A model Pincer cannot send effort to (an old Claude, a custom name) would otherwise warn
    every operator who configured nothing."""
    caplog.set_level(logging.DEBUG)
    settings.anthropic_model = "claude-3-5-haiku-20241022"  # unrecognised: both pinned values are dropped
    async with ScriptedAnthropic(settings, _JSON_REPLY) as llm:
        await extract_outcome(llm.provider, "AGENT: hi\nCALLER: bye", "")

    records = _dropped(caplog, "claude-3-5-haiku-20241022")
    assert records, "precondition: the pin was dropped and logged"
    assert {r.levelno for r in records} == {logging.DEBUG}


async def test_a_dropped_pin_is_never_a_warning_even_when_the_operator_set_something_globally(
    structured_request, caplog
):
    """With a global adaptive thinking, "Not sending thinking=disabled" would name a value the operator never set."""
    caplog.set_level(logging.DEBUG)
    request = await structured_request("claude-opus-5-5")  # a model that cannot turn thinking off

    assert "thinking" not in request or request["thinking"]["type"] != "disabled"  # precondition: it was dropped
    records = _dropped(caplog, "claude-opus-5-5")
    assert records, "precondition: the pin was dropped and logged"
    assert {r.levelno for r in records} == {logging.DEBUG}
