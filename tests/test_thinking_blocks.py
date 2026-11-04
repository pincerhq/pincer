"""Thinking blocks ride along within one agent turn and are gone by the next (issue #345)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, redacted, text, thinking, tool_use

from pincer.core.agent import Agent, StreamEventType, _sanitize_tool_pairs
from pincer.core.session import SessionManager
from pincer.exceptions import BudgetExceededError, LLMError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    StreamTurnEvent,
    ThinkingBlock,
    ToolCall,
)
from pincer.tools.registry import ToolRegistry

# Sentinels: none of these may ever leave the model round-trip
_THOUGHT = "SECRET-THOUGHT-7f3a"
_SIGNATURE = "SIG-7f3a-signature"
_REDACTED = "REDACTED-7f3a-data"
_SENTINELS = (_THOUGHT, _SIGNATURE, _REDACTED)


class _Tools:
    """A registry with one tool that records its runs."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.registry = ToolRegistry()

        async def create_event(title: str = "") -> str:
            self.runs.append(title)
            return "created"

        self.registry.register(name="create_event", description="Create a calendar event", handler=create_event)


@pytest.fixture
def tools() -> _Tools:
    return _Tools()


def _block_types(request: dict[str, Any]) -> list[str]:
    return [
        block["type"]
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ]


def _assert_no_sentinel(where: str, value: str) -> None:
    for sentinel in _SENTINELS:
        assert sentinel not in value, f"thinking leaked into {where}"


def _assert_no_sentinel_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    pincer_records = [r for r in caplog.records if r.name == "pincer" or r.name.startswith("pincer.")]
    assert pincer_records, "precondition: the pincer loggers were captured"
    for record in pincer_records:
        _assert_no_sentinel(f"log {record.name}", record.getMessage())


async def test_thinking_rides_along_within_a_turn_and_is_gone_by_the_next(
    settings, session_manager, cost_tracker, tools, caplog
):
    script = [
        (
            [
                thinking(_THOUGHT, _SIGNATURE),
                redacted(_REDACTED),
                tool_use("toolu_1", "create_event", {"title": "Dentist"}),
            ],
            "tool_use",
        ),
        ([text("Booked.")], "end_turn"),
        ([text("You're welcome.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            first = await agent.handle_message("user1", "test", "Book the dentist", memory=False)
            session = await session_manager.get_or_create("user1", "test")
            second = await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert tools.runs == ["Dentist"]
    assert len(llm.requests) == 3
    assistant = [m for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"][:2] == [
        {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
        {"type": "redacted_thinking", "data": _REDACTED},
    ]
    # Turn 2 runs on the same cached session object, and sends no thinking
    assert await session_manager.get_or_create("user1", "test") is session
    assert "thinking" not in _block_types(llm.requests[2])
    assert "redacted_thinking" not in _block_types(llm.requests[2])
    _assert_no_sentinel("turn 2's request", llm.raw_requests[2])
    assert all(not m.thinking_blocks for m in session.messages)
    # Never visible: channel text, saved session, logs
    _assert_no_sentinel("channel text", first.text + second.text)
    stored = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert stored is not None
    _assert_no_sentinel("saved session", json.dumps(stored["messages"]))
    _assert_no_sentinel_in_logs(caplog)


_THINKING = [
    ThinkingBlock("thinking", thinking=_THOUGHT, signature=_SIGNATURE),
    ThinkingBlock("redacted_thinking", data=_REDACTED),
]
_DONE = LLMResponse(content="done", model="test-model", input_tokens=10, output_tokens=5, stop_reason="end_turn")


def _thinking_tool_response(tool_id: str = "tc1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=tool_id, name="create_event", arguments={"title": "Dentist"})],
        model="test-model",
        input_tokens=50,
        output_tokens=40,
        stop_reason="tool_use",
        thinking_blocks=list(_THINKING),
    )


def _sent_thinking(messages: list[LLMMessage]) -> list[ThinkingBlock]:
    """The thinking blocks a request carried, read when it was made."""
    return [block for m in messages for block in m.thinking_blocks]


async def test_streaming_chat_final_stream_gets_the_turns_thinking(
    settings, mock_llm, session_manager, cost_tracker, tools, caplog
):
    mock_llm.complete.side_effect = [_thinking_tool_response(), _DONE]
    streamed_with: list[list[ThinkingBlock]] = []

    async def stream(messages, **kwargs):
        streamed_with.append(_sent_thinking(messages))
        yield "Booked."

    mock_llm.stream = stream
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    with caplog.at_level(logging.DEBUG, logger="pincer"):
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]

    assert tools.runs == ["Dentist"]
    assert streamed_with == [_THINKING]
    session = await session_manager.get_or_create("user1", "web")
    assert _sent_thinking(session.messages) == []
    for chunk in chunks:
        _assert_no_sentinel(f"{chunk.type} chunk", chunk.content)
    _assert_no_sentinel_in_logs(caplog)


class _ScriptedVoice(BaseLLMProvider):
    """stream_turn replays scripted turns, one per request, recording the thinking each request carried."""

    def __init__(self, turns: list[list[StreamTurnEvent]]) -> None:
        self.turns = list(turns)
        self.sent: list[list[ThinkingBlock]] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use stream()")
        yield ""  # pragma: no cover

    async def stream_turn(
        self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None, reasoning_effort=None
    ):
        self.sent.append(_sent_thinking(messages))
        for event in self.turns.pop(0):
            yield event

    async def close(self) -> None:
        pass


def _voice_agent(settings, provider, session_manager, cost_tracker, tools) -> Agent:
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    return Agent(settings, provider, session_manager, cost_tracker, tools.registry)


async def test_voice_follow_up_request_gets_the_turns_thinking(settings, session_manager, cost_tracker, tools, caplog):
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(text="Einen Moment. "), StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    with caplog.at_level(logging.DEBUG, logger="pincer"):
        chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]

    assert tools.runs == ["Dentist"]
    assert provider.sent == [[], _THINKING]
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert _sent_thinking(session.messages) == []
    # The phone channel speaks and transcribes only these chunks
    for chunk in chunks:
        _assert_no_sentinel(f"{chunk.type} chunk", chunk.content)
    _assert_no_sentinel_in_logs(caplog)


async def test_voice_cut_off_retry_never_sends_the_discarded_thinking(settings, session_manager, cost_tracker, tools):
    discarded = [ThinkingBlock("thinking", thinking="DISCARDED-THOUGHT", signature="DISCARDED-SIG")]
    cut_off = LLMResponse(
        content="",
        model="test-model",
        input_tokens=50,
        output_tokens=220,
        stop_reason="max_tokens",
        thinking_blocks=discarded,
    )
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(response=cut_off)],  # nothing streamed: retried silently
            [StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]

    assert chunks[-1].content == "Erledigt."
    assert provider.sent == [[], [], _THINKING]


async def test_chat_turn_strips_thinking_when_it_fails(settings, mock_llm, session_manager, cost_tracker, tools):
    mock_llm.complete.side_effect = [_thinking_tool_response(), LLMError("Anthropic API error: 500 overloaded"), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    session = await session_manager.get_or_create("user1", "test")
    assert any(m.tool_calls for m in session.messages)  # precondition: the thinking message was saved
    assert _sent_thinking(session.messages) == []


async def test_streaming_chat_strips_thinking_on_a_budget_stop(
    settings, mock_llm, session_manager, cost_tracker, tools
):
    mock_llm.complete.side_effect = [_thinking_tool_response(), BudgetExceededError(100.0, 100.0), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    chunks = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]

    assert chunks[-1].content == "Daily budget limit reached."  # precondition: the budget stop's early return
    session = await session_manager.get_or_create("user1", "web")
    assert any(m.tool_calls for m in session.messages)
    assert _sent_thinking(session.messages) == []


async def test_voice_turn_strips_thinking_when_the_caller_hangs_up_mid_turn(
    settings, session_manager, cost_tracker, tools
):
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    stream = agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")
    async for chunk in stream:
        if chunk.type is StreamEventType.TOOL_START:
            break  # the caller hung up: the turn is closed mid-tool-loop
    await stream.aclose()

    assert len(provider.sent) == 1  # precondition: closed before the follow-up request
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert any(m.tool_calls for m in session.messages)
    assert _sent_thinking(session.messages) == []


async def test_saved_session_is_unchanged_by_thinking(session_manager, tmp_path):
    def history(thinking_blocks: list[ThinkingBlock]) -> list[LLMMessage]:
        call = ToolCall(id="tc1", name="create_event", arguments={"title": "Dentist"})
        return [
            LLMMessage(role=MessageRole.USER, content="Book the dentist"),
            LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call], thinking_blocks=thinking_blocks),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="tc1"),
        ]

    for user_id, blocks in (("with", list(_THINKING)), ("without", [])):
        session = await session_manager.get_or_create(user_id, "test")
        for message in history(blocks):
            await session_manager.add_message(session, message)
    assert _sent_thinking((await session_manager.get_or_create("with", "test")).messages) == _THINKING  # precondition

    rows = {
        user_id: await session_manager._store.load(
            f"test:{user_id}", user_id=user_id, channel="test", legacy_fallback=False
        )
        for user_id in ("with", "without")
    }
    assert rows["with"] is not None and rows["without"] is not None
    assert rows["with"]["messages"] == rows["without"]["messages"]
    _assert_no_sentinel("saved session", rows["with"]["messages"])

    reloaded = SessionManager(tmp_path / "test.db")
    await reloaded.initialize()
    try:
        session = await reloaded.get_or_create("with", "test")
        assert len(session.messages) == 3
        assert _sent_thinking(session.messages) == []
    finally:
        await reloaded.close()


def test_repaired_tool_call_message_drops_its_stale_thinking():
    answered = ToolCall(id="tc1", name="create_event", arguments={"title": "Dentist"})
    orphaned = ToolCall(id="tc2", name="create_event", arguments={"title": "Doctor"})
    messages = [
        LLMMessage(role=MessageRole.USER, content="Book both"),
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="Booking.",
            tool_calls=[answered, orphaned],
            thinking_blocks=list(_THINKING),
        ),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="tc1"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[orphaned], thinking_blocks=list(_THINKING)),
        LLMMessage(role=MessageRole.USER, content="And?"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [m.tool_calls for m in clean if m.role is MessageRole.ASSISTANT] == [[answered]]  # precondition: repaired
    assert _sent_thinking(clean) == []
