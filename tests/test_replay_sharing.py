"""A request shares the stored provider output instead of copying it on every request (PR #379 review)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic

from pincer.llm.base import LLMMessage, MessageRole, ToolCall
from pincer.llm.openai_responses import convert_messages_to_responses
from pincer.llm.replay import replayable_raw

_ANTHROPIC_WIRE = "anthropic:claude-haiku-4-5-20251001"
_OPENAI_WIRE = "openai:gpt-6"


def _anthropic_history() -> list[LLMMessage]:
    call = ToolCall(id="toolu_1", name="search", arguments={"q": "x"})
    raw = [
        {"type": "thinking", "thinking": "t" * 500, "signature": "SIG"},
        {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
    ]
    return [
        LLMMessage(role=MessageRole.USER, content="look it up"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call], raw=raw, wire=_ANTHROPIC_WIRE),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="found", tool_call_id="toolu_1"),
    ]


def _responses_history() -> list[LLMMessage]:
    raw = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC" * 100},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]},
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "TRAILING"},
    ]
    return [
        LLMMessage(role=MessageRole.USER, content="hello"),
        LLMMessage(role=MessageRole.ASSISTANT, content="Hi", raw=raw, wire=_OPENAI_WIRE),
    ]


@pytest.fixture
def no_deepcopy(monkeypatch):
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the stored provider output was deep-copied")

    monkeypatch.setattr(copy, "deepcopy", refuse)


async def test_an_anthropic_request_does_not_deep_copy_the_stored_blocks(settings, no_deepcopy):
    async with ScriptedAnthropic(settings, []) as llm:
        converted = llm.provider._convert_messages(_anthropic_history(), "claude-haiku-4-5-20251001")

    assert converted[1]["content"][0]["signature"] == "SIG"  # precondition: the thinking block was replayed


def test_a_responses_request_does_not_deep_copy_the_stored_items(no_deepcopy):
    items = convert_messages_to_responses(_responses_history(), _OPENAI_WIRE)

    assert [i.get("id") for i in items if i.get("type") == "reasoning"] == ["rs_1"]  # precondition: replayed


def test_the_replayed_list_is_the_requests_own_but_the_stored_output_is_never_changed():
    messages = _responses_history()
    stored = copy.deepcopy(messages[1].raw)

    first = convert_messages_to_responses(messages, _OPENAI_WIRE)
    second = convert_messages_to_responses(messages, _OPENAI_WIRE)

    assert messages[1].raw == stored  # the trailing reasoning item was dropped from the request, not the store
    assert first == second
    assert [i.get("type", i.get("role")) for i in first] == ["user", "reasoning", "message"]  # trailing one dropped


def test_an_anthropic_request_leaves_the_stored_output_as_it_was(settings):
    messages = _anthropic_history()
    stored = copy.deepcopy(messages[1].raw)
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    provider = AnthropicCompatibleProvider(settings, "anthropic")
    first = provider._convert_messages(messages, "claude-haiku-4-5-20251001")
    second = provider._convert_messages(messages, "claude-haiku-4-5-20251001")

    assert messages[1].raw == stored
    assert first == second


def test_replayable_raw_returns_a_new_list_of_the_same_blocks_and_none_for_another_wire():
    message = _anthropic_history()[1]

    replayed = replayable_raw(message, _ANTHROPIC_WIRE)

    assert replayed == message.raw
    assert replayed is not message.raw  # popping from it must not change the message
    assert replayable_raw(message, "anthropic:another-model") is None
    assert replayable_raw(LLMMessage(role=MessageRole.ASSISTANT, content="x"), _ANTHROPIC_WIRE) is None
