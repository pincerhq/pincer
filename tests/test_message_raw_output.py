"""An assistant message keeps its provider's output and the wire that produced it (issue #358, slice 1)."""

from __future__ import annotations

import json

from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.llm.replay import replayable_raw, wire_tag

_THINKING = {"type": "thinking", "thinking": "SECRET-THOUGHT-7f3a", "signature": "SIG-7f3a"}
_TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {"title": "Dentist"}}
_CALL = ToolCall(id="toolu_1", name="create_event", arguments={"title": "Dentist"})
_WIRE = "foundry:claude-haiku-5-5"


def _assistant(raw: list[dict] | None = None, wire: str = _WIRE) -> LLMMessage:
    return LLMMessage(
        role=MessageRole.ASSISTANT,
        content="Checking.",
        tool_calls=[_CALL],
        raw=[_THINKING, {"type": "text", "text": "Checking."}, _TOOL_USE] if raw is None else raw,
        wire=wire,
    )


def test_raw_and_wire_survive_a_save_and_a_load():
    stored = json.loads(json.dumps(_assistant().to_dict()))

    assert "raw" in stored and "wire" in stored  # precondition: they were written
    loaded = LLMMessage.from_dict(stored)

    assert loaded.raw == _assistant().raw
    assert loaded.wire == _WIRE
    assert loaded.tool_calls == [_CALL]


def test_a_message_without_raw_is_stored_as_it_always_was():
    message = LLMMessage(role=MessageRole.USER, content="hi")

    assert message.to_dict() == {"role": "user", "content": "hi"}
    assert LLMMessage.from_dict({"role": "user", "content": "hi"}).raw == []


def test_a_message_built_from_a_response_keeps_its_output_and_wire():
    response = LLMResponse(content="Checking.", tool_calls=[_CALL], raw=[_THINKING, _TOOL_USE], wire=_WIRE)

    message = LLMMessage.from_response(response)

    assert (message.role, message.content, message.tool_calls) == (MessageRole.ASSISTANT, "Checking.", [_CALL])
    assert (message.raw, message.wire) == ([_THINKING, _TOOL_USE], _WIRE)
    assert LLMMessage.from_response(response, content="Edited.").content == "Edited."


def test_the_replay_rule_sends_raw_only_to_the_wire_that_produced_it():
    message = _assistant()

    assert replayable_raw(message, _WIRE) == message.raw
    assert replayable_raw(message, "foundry:claude-sonnet-5-5") is None
    assert replayable_raw(message, "anthropic:claude-haiku-5-5") is None
    assert replayable_raw(_assistant(raw=[]), _WIRE) is None
    assert wire_tag("foundry", "claude-haiku-5-5") == _WIRE


def test_the_replayed_output_is_a_copy_the_caller_may_change():
    message = _assistant()
    replayed = replayable_raw(message, _WIRE)
    assert replayed is not None  # precondition: it replays

    replayed[2]["input"]["title"] = "Changed"
    replayed.append({"type": "text", "text": "extra"})

    assert message.raw == [_THINKING, {"type": "text", "text": "Checking."}, _TOOL_USE]
