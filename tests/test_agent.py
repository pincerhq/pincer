"""Tests for the ReAct agent loop."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.config import Settings
from pincer.core.agent import (
    _CALL_PLACED_THEN_FAILED_TEXT,
    _HEADLESS_LLM_ERROR_TEXT,
    _SESSION_CLEARED_TEXT,
    MEMORY_BLOCK_HEADER,
    Agent,
    AgentResponse,
    StreamChunk,
    StreamEventType,
    _is_tool_pair_error,
    _placed_call_sid,
    _sanitize_tool_pairs,
)
from pincer.core.session import SessionManager
from pincer.exceptions import LLMError, LLMOutputTruncatedError
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, StreamTurnEvent, ToolCall, ToolResult
from pincer.memory.mcp import MCPMemoryBackend
from pincer.memory.summarizer import Summarizer
from pincer.tools.builtin import files
from pincer.tools.registry import ToolRegistry
from pincer.tools.selection import ToolSelector

_DONE = LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn")
_OPENAI_UNPAIRED = (
    "openai API error: 400 An assistant message with 'tool_calls' must be followed by tool messages "
    "responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: late"
)
_ANTHROPIC_UNPAIRED = (
    "Anthropic API error: 400 messages.1: `tool_use` ids were found without `tool_result` blocks immediately after"
)
_ANTHROPIC_NO_TOOLS = (
    "Anthropic API error: 400 Requests which include `tool_use` or `tool_result` blocks must define tools."
)


async def test_turns_on_same_session_wait_and_other_sessions_continue(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = agent._handle_message

    async def blocked(user_id, channel, text, *args, **kwargs):
        if text == "first":
            entered.set()
            await release.wait()
        return await original(user_id, channel, text, *args, **kwargs)

    with patch.object(agent, "_handle_message", side_effect=blocked):
        first = asyncio.create_task(agent.handle_message("user", "chat", "first", memory=False))
        await entered.wait()
        second = asyncio.create_task(agent.handle_message("user", "chat", "second", memory=False))
        await asyncio.sleep(0)
        assert not second.done()
        await agent.handle_message("other", "chat", "other", memory=False)
        release.set()
        await asyncio.gather(first, second)

    session = await session_manager.get_or_create("user", "chat")
    assert [m.content for m in session.messages if m.role == MessageRole.USER] == ["first", "second"]
    assert not session_manager._turn_locks


async def test_cancelled_waiter_does_not_release_active_turn(session_manager):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with session_manager.turn("user", "chat"):
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await entered.wait()
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert "chat:user" in session_manager._turn_locks
    release.set()
    await first
    assert not session_manager._turn_locks


async def test_explicit_session_key_serializes_across_channels(session_manager):
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with session_manager.turn("user-a", "channel-a", "shared"):
            entered.set()
            await release.wait()

    async def second():
        async with session_manager.turn("user-b", "channel-b", "shared"):
            second_entered.set()

    active = asyncio.create_task(first())
    await entered.wait()
    queued = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release.set()
    await asyncio.gather(active, queued)
    assert second_entered.is_set()
    assert not session_manager._turn_locks


@pytest.mark.parametrize("voice", [False, True])
async def test_closing_stream_releases_session_for_next_turn(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, voice
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    closed = asyncio.Event()

    async def stream(*args, **kwargs):
        try:
            yield StreamChunk(StreamEventType.TEXT, "partial")
        finally:
            closed.set()

    method = "_stream_voice_turn" if voice else "_handle_message_stream"
    with patch.object(agent, method, side_effect=stream):
        output = (
            agent.stream_voice_turn("user", "chat", "first")
            if voice
            else agent.handle_message_stream("user", "chat", "first")
        )
        await anext(output)
        waiting = asyncio.create_task(agent.handle_message("user", "chat", "second", memory=False))
        await asyncio.sleep(0)
        assert not waiting.done()
        await output.aclose()
        await waiting
    assert closed.is_set()
    assert not session_manager._turn_locks


async def test_end_voice_session_can_run_inside_voice_turn(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    session_key = "voice:call"

    async def stream(*args, **kwargs):
        await agent.end_voice_session(session_key)
        yield StreamChunk(StreamEventType.DONE, "finished")

    with patch.object(agent, "_stream_voice_turn", side_effect=stream):
        output = agent.stream_voice_turn("user", "voice", "finished", session_key=session_key)
        chunk = await asyncio.wait_for(anext(output), timeout=1)
        await output.aclose()

    assert chunk.type == StreamEventType.DONE
    assert not session_manager._turn_locks


async def test_approval_wait_preserves_tool_results_before_next_turn(settings, mock_llm, session_manager, cost_tracker):
    entered = asyncio.Event()
    release = asyncio.Event()
    registry = ToolRegistry()

    async def risky() -> str:
        return "tool output"

    async def approve(*args):
        entered.set()
        await release.wait()
        return True

    registry.register(name="risky", description="risky", handler=risky, require_approval=True)
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="risky", arguments={})],
            model="test",
            input_tokens=10,
            output_tokens=5,
            stop_reason="tool_use",
        ),
        _DONE,
        _DONE,
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry, approval_callback=approve)
    first = asyncio.create_task(agent.handle_message("user", "chat", "first", memory=False))
    await entered.wait()
    second = asyncio.create_task(agent.handle_message("user", "chat", "second", memory=False))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    session = await session_manager.get_or_create("user", "chat")
    roles = [m.role for m in session.messages]
    assert roles == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL_RESULT,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert session.messages[2].tool_call_id == "tc1"


@pytest.mark.asyncio
async def test_simple_response(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Hello!")
    assert isinstance(result, AgentResponse)
    assert "Hello! I'm Pincer." in result.text
    assert result.tool_calls_made == 0


@pytest.mark.asyncio
async def test_tool_call_loop(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    # First call: LLM wants to use a tool
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        # Second call: LLM produces final text after seeing tool result
        LLMResponse(
            content="The greeting is: Hello, World!",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Greet the world")

    assert "Hello, World!" in result.text
    assert result.tool_calls_made == 1
    assert mock_llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_tool_not_found(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="Sorry, that tool isn't available.",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Use nonexistent tool")
    # Agent should handle gracefully — the LLM gets an error and responds
    assert "Sorry, that tool isn't available." in result.text


_UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _without_turn_id(contexts: list[dict]) -> list[dict]:
    """The contexts minus ``turn_id`` (asserted to be a UUID7: it differs every turn)."""
    stripped = []
    for ctx in contexts:
        assert _UUID7_RE.match(str(ctx.get("turn_id"))), ctx
        stripped.append({k: v for k, v in ctx.items() if k != "turn_id"})
    return stripped


def _context_capturing_registry() -> tuple[ToolRegistry, list[dict]]:
    """A registry with one tool that records the `context` dict it was called with.

    Mirrors channel-bound tools like send_file/send_image, which read
    context["user_id"] expecting the channel-native ID (issue #162).
    """
    registry = ToolRegistry()
    seen_contexts: list[dict] = []

    async def whoami(context: dict | None = None) -> str:
        seen_contexts.append(context or {})
        return "ok"

    registry.register(
        name="whoami",
        description="Records the context it receives",
        handler=whoami,
        parameters={"type": "object", "properties": {}},
    )
    return registry, seen_contexts


@pytest.mark.asyncio
async def test_tool_context_prefers_channel_user_id(settings, mock_llm, session_manager, cost_tracker):
    """Channel-bound tools must see the channel-native ID, not the canonical user_id (issue #162)."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    await agent.handle_message(
        user_id="canonical-r_lutsiv",
        channel="test",
        text="who am i",
        channel_user_id="123456789",
    )

    assert _without_turn_id(seen_contexts) == [
        {
            "user_id": "123456789",
            "channel": "test",
            "pincer_user_id": "canonical-r_lutsiv",
            "channel_name": "test",
            "timezone": "",
            "tool_call_id": "tc1",
            "session_key": "test:canonical-r_lutsiv",
        }
    ]


@pytest.mark.asyncio
async def test_tool_context_falls_back_to_user_id_when_no_channel_user_id(
    settings, mock_llm, session_manager, cost_tracker
):
    """Without a channel_user_id (e.g. CLI/local chat), the canonical user_id still works."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    await agent.handle_message(user_id="user1", channel="test", text="who am i")

    assert _without_turn_id(seen_contexts) == [
        {
            "user_id": "user1",
            "channel": "test",
            "pincer_user_id": "user1",
            "channel_name": "test",
            "timezone": "",
            "tool_call_id": "tc1",
            "session_key": "test:user1",
        }
    ]


@pytest.mark.asyncio
async def test_tool_context_carries_the_users_identity_timezone(settings, mock_llm, session_manager, cost_tracker):
    """Calendar tools read offset-less times in the user's zone when the calendar's is unknown."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(content="done", model="test", input_tokens=80, output_tokens=20, stop_reason="end_turn"),
    ]
    looked_up: list[str] = []

    class _Identity:
        async def get_timezone(self, pincer_user_id: str) -> str:
            looked_up.append(pincer_user_id)
            return "Europe/Kyiv"

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    agent.identity_resolver = _Identity()
    await agent.handle_message(
        user_id="canonical-r_lutsiv", channel="test", text="who am i", channel_user_id="123456789"
    )

    assert [ctx.get("timezone") for ctx in seen_contexts] == ["Europe/Kyiv"]
    assert "canonical-r_lutsiv" in looked_up


@pytest.mark.parametrize(
    ("name", "is_error", "sid"),
    [("make_phone_call", False, "CA9"), ("make_phone_call", True, ""), ("whoami", False, "")],
)
def test_only_a_make_phone_call_result_names_a_placed_call(name, is_error, sid):
    # The result format itself is tested against the real dial paths (test_voice_briefing.py).
    result = ToolResult(tool_call_id="tc1", content=_DIAL_RESULTS[1], is_error=is_error)
    assert _placed_call_sid(name, result) == sid


def _dialing_registry(results: list[str]) -> ToolRegistry:
    """A registry whose make_phone_call returns `results` in turn."""
    registry = ToolRegistry()
    pending = iter(results)

    async def make_phone_call(context: dict | None = None) -> str:
        return next(pending)

    registry.register(
        name="make_phone_call",
        description="Places a call",
        handler=make_phone_call,
        parameters={"type": "object", "properties": {}},
    )
    return registry


def _two_dials_then_text() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="tc1", name="make_phone_call", arguments={}),
                ToolCall(id="tc2", name="make_phone_call", arguments={}),
            ],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(content="ringing", model="test", input_tokens=80, output_tokens=20, stop_reason="end_turn"),
    ]


_DIAL_RESULTS = [
    "Error: Daily outbound call limit reached (10). Try again tomorrow.",
    "Call initiated successfully.\nCall SID: CA9\nTo: +4930123\nPurpose: x\n",
]


@pytest.mark.asyncio
async def test_a_turn_returns_the_calls_it_placed(settings, mock_llm, session_manager, cost_tracker):
    mock_llm.complete.side_effect = _two_dials_then_text()
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    result = await agent.handle_message("user1", "test", "call them")

    assert result.call_sids == ["CA9"]


@pytest.mark.asyncio
async def test_a_turn_that_fails_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    """An error response would hide the ringing call from web chat, which then
    never waits for its report; the turn ends with a reply instead."""
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], LLMError("overloaded 529")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    result = await agent.handle_message("user1", "test", "call them")

    assert result.call_sids == ["CA9"]
    assert "placed the call" in result.text


@pytest.mark.asyncio
async def test_a_turn_that_fails_before_placing_a_call_still_raises(settings, mock_llm, session_manager, cost_tracker):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], LLMError("overloaded 529")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "call them")


def _cut_off_dial() -> LLMResponse:
    """A response stopped by max_tokens inside a tool call: its input may be incomplete."""
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id="tc3", name="make_phone_call", arguments={})],
        model="test",
        input_tokens=80,
        output_tokens=4096,
        stop_reason="max_tokens",
    )


@pytest.mark.asyncio
async def test_a_turn_cut_off_after_placing_a_call_still_returns_it(settings, mock_llm, session_manager, cost_tracker):
    """The cut-off guard must not hide a ringing call either: the turn ends with
    the placed-call reply, and the cut-off tool call never runs."""
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _cut_off_dial()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    result = await agent.handle_message("user1", "test", "call them")

    assert result.call_sids == ["CA9"]
    assert "placed the call" in result.text
    session = await session_manager.get_or_create("user1", "test")
    assert all(tc.id != "tc3" for m in session.messages for tc in m.tool_calls)
    assert all(m.tool_call_id != "tc3" for m in session.messages)


@pytest.mark.asyncio
async def test_a_turn_cut_off_before_placing_a_call_still_raises(settings, mock_llm, session_manager, cost_tracker):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _cut_off_dial()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(LLMOutputTruncatedError):
        await agent.handle_message("user1", "test", "call them")


def _cut_off_before_any_text(text: str = "") -> LLMResponse:
    """A thinking model spent its whole max_tokens budget: stopped on max_tokens with no tool call."""
    return LLMResponse(
        content=text, model="test", input_tokens=80, output_tokens=4096, stop_reason="max_tokens", provider="test"
    )


@pytest.mark.asyncio
async def test_a_reply_cut_off_before_any_text_is_an_error_not_an_empty_answer(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_cut_off_before_any_text()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMOutputTruncatedError):
        await agent.handle_message("user1", "test", "think hard")


@pytest.mark.asyncio
async def test_a_reply_cut_off_mid_sentence_is_still_delivered(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_cut_off_before_any_text("The answer is long and be")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.handle_message("user1", "test", "explain", memory=False)

    assert result.text == "The answer is long and be"


@pytest.mark.asyncio
async def test_a_headless_run_cut_off_before_any_text_returns_the_error_text(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_cut_off_before_any_text()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("Say hi", user_id="usr1", channel="scheduled")

    assert result == _HEADLESS_LLM_ERROR_TEXT


@pytest.mark.asyncio
async def test_a_streamed_reply_cut_off_before_any_text_raises(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The reply is the final stream: when that one is cut off before any text, the turn raises."""

    async def stream_turn(**kwargs):
        yield StreamTurnEvent(response=_cut_off_before_any_text())

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "think hard")]


def _stream_turn_fails_before_its_first_event(mock_llm) -> None:
    async def stream_turn(**kwargs):
        raise RuntimeError("connection reset")
        yield  # pragma: no cover

    mock_llm.stream_turn = stream_turn


@pytest.mark.asyncio
async def test_a_failed_final_stream_never_falls_back_to_a_cut_off_blank_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The tool loop lets a blank cut-off through because the final stream is the reply; when that
    stream fails before its first event, the fallback must not send the blank response as the reply."""
    mock_llm.complete.side_effect = [_cut_off_before_any_text()]
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "think hard", memory=False)]


@pytest.mark.asyncio
async def test_a_failed_final_stream_never_falls_back_to_a_blank_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [
        LLMResponse(content="", model="test", input_tokens=80, output_tokens=1, stop_reason="end_turn")
    ]
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_a_failed_final_stream_falls_back_to_the_tool_loop_text(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [
        LLMResponse(content="Hello there", model="test", input_tokens=80, output_tokens=5, stop_reason="end_turn")
    ]
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]

    assert chunks[-1].type == StreamEventType.DONE
    assert chunks[-1].content == "Hello there"


def _dials_then_a_blank_reply() -> list[LLMResponse]:
    """The tool loop: a batch of dials, then a response with no tool call and no text (the stream is the reply)."""
    dials, _ringing = _two_dials_then_text()
    blank = LLMResponse(content="", model="test", input_tokens=80, output_tokens=1, stop_reason="end_turn")
    return [dials, blank]


async def _assert_the_placed_call_text_is_the_reply(agent, session_manager) -> None:
    chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]

    assert chunks[-1].type == StreamEventType.DONE
    assert chunks[-1].content == _CALL_PLACED_THEN_FAILED_TEXT
    assert "".join(c.content for c in chunks if c.type == StreamEventType.TEXT) == _CALL_PLACED_THEN_FAILED_TEXT
    session = await session_manager.get_or_create("user1", "test")
    assert [m.role for m in session.messages[-2:]] == [MessageRole.TOOL_RESULT, MessageRole.ASSISTANT]
    assert session.messages[-1].content == _CALL_PLACED_THEN_FAILED_TEXT


@pytest.mark.asyncio
async def test_a_streamed_reply_cut_off_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    """As on the non-stream path: a ringing call must reach the channel, so the cut-off ends the turn with a reply."""
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()

    async def stream_turn(**kwargs):
        yield StreamTurnEvent(response=_cut_off_before_any_text())

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_a_failed_final_stream_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_a_streamed_reply_cut_off_before_placing_a_call_still_raises(
    settings, mock_llm, session_manager, cost_tracker
):
    """The first dial fails (the limit), so no call was placed."""
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()

    async def stream_turn(**kwargs):
        yield StreamTurnEvent(response=_cut_off_before_any_text())

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]


async def _stream_closed_without_a_final_response(**kwargs):
    return
    yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_stream_closed_without_text_or_a_final_response_is_an_error_not_an_empty_answer(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    mock_llm.stream_turn = _stream_closed_without_a_final_response
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError, match="closed without a final response"):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_a_stream_closed_without_a_final_response_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    mock_llm.stream_turn = _stream_closed_without_a_final_response
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_streamed_text_is_delivered_and_saved_when_the_stream_closes_without_a_final_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """The user has already seen these chunks: the reply is what was streamed."""

    async def stream_turn(**kwargs):
        yield StreamTurnEvent(text="Hello ")
        yield StreamTurnEvent(text="there")

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"):
        chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]

    assert chunks[-1].content == "Hello there"
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[-1].role == MessageRole.ASSISTANT
    assert session.messages[-1].content == "Hello there"
    assert "without a final response" in caplog.text


@pytest.mark.asyncio
async def test_a_streamed_turn_whose_next_request_fails_after_a_dial_still_returns_the_placed_call(
    settings, mock_llm, session_manager, cost_tracker
):
    """The tool loop's own request fails after a dial: the ringing call must still reach the channel."""
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], LLMError("overloaded 529")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_a_streamed_turn_cut_off_inside_a_tool_call_after_a_dial_still_returns_the_placed_call(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _cut_off_dial()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)

    session = await session_manager.get_or_create("user1", "test")
    assert all(tc.id != "tc3" for m in session.messages for tc in m.tool_calls)  # the cut-off call never ran
    assert all(m.tool_call_id != "tc3" for m in session.messages)


@pytest.mark.asyncio
async def test_a_streamed_turn_whose_next_request_fails_before_any_dial_still_raises(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], LLMError("overloaded 529")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(LLMError, match="overloaded"):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]


@pytest.mark.asyncio
async def test_a_streamed_turn_cut_off_inside_a_tool_call_before_any_dial_still_raises(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _cut_off_dial()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]


@pytest.mark.asyncio
async def test_a_streamed_turn_over_budget_after_a_dial_keeps_the_budget_reply(
    settings, mock_llm, session_manager, tmp_path
):
    """A budget stop is not an LLM failure: it keeps its own reply, never the placed-call text.
    The dialling request crosses the budget ($7/MTok x 1,000 = the whole $0.007)."""
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    dial = _two_dials_then_text()[0]
    dial.request_model, dial.input_tokens, dial.output_tokens = "test-fixture-model", 1_000, 0
    mock_llm.complete.side_effect = [dial, _two_dials_then_text()[1]]
    tracker = CostTracker(tmp_path / "test.db", daily_budget=0.007, prices=load_price_table())
    await tracker.initialize()
    agent = Agent(
        settings.model_copy(update={"daily_budget_usd": 0.007}),
        mock_llm,
        session_manager,
        tracker,
        _dialing_registry(_DIAL_RESULTS),
    )

    try:
        chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]
    finally:
        await tracker.close()

    assert mock_llm.complete.await_count == 1
    assert chunks[-1].type == StreamEventType.DONE
    assert chunks[-1].content.startswith("Warning: Daily budget limit reached.")
    assert [c.call_sid for c in chunks if c.call_sid] == ["CA9"]


def _stream_of(*events: StreamTurnEvent):
    async def stream_turn(**kwargs):
        for event in events:
            yield event

    return stream_turn


_BLANK_FINAL = StreamTurnEvent(
    response=LLMResponse(content="", model="test", input_tokens=80, output_tokens=1, stop_reason="end_turn")
)


@pytest.mark.asyncio
async def test_a_completed_stream_with_no_text_is_an_error_not_an_empty_answer(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    mock_llm.stream_turn = _stream_of(_BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_a_completed_stream_of_only_whitespace_is_an_error(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="  "), StreamTurnEvent(text="\n"), _BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_whitespace_streamed_with_no_final_response_is_an_error(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="  \n"))
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_a_completed_blank_stream_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    mock_llm.stream_turn = _stream_of(_BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_a_completed_stream_with_text_is_delivered_and_saved(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    final = StreamTurnEvent(
        response=LLMResponse(
            content="Hello there", model="test", input_tokens=80, output_tokens=5, stop_reason="end_turn"
        )
    )
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="Hello "), StreamTurnEvent(text="there"), final)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]

    assert chunks[-1].content == "Hello there"
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[-1].content == "Hello there"


async def _placed_call_reply_chunks(agent, session_manager) -> tuple[list[str], str, str]:
    """(the TEXT chunks the client received, the DONE text, the saved assistant message)."""
    chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]
    session = await session_manager.get_or_create("user1", "test")
    assert chunks[-1].type == StreamEventType.DONE
    assert session.messages[-1].role == MessageRole.ASSISTANT
    return (
        [c.content for c in chunks if c.type == StreamEventType.TEXT],
        chunks[-1].content,
        session.messages[-1].content,
    )


@pytest.mark.asyncio
async def test_the_placed_call_text_is_appended_to_text_already_streamed(
    settings, mock_llm, session_manager, cost_tracker
):
    """The client already received "Ringing now": DONE, the saved message and the chunks must all agree."""
    cut_off = LLMResponse(
        content="Ringing now",
        tool_calls=[ToolCall(id="tc9", name="make_phone_call", arguments={})],
        model="test",
        input_tokens=80,
        output_tokens=4096,
        stop_reason="max_tokens",
    )
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="Ringing now"), StreamTurnEvent(response=cut_off))
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    texts, done, saved = await _placed_call_reply_chunks(agent, session_manager)

    expected = "Ringing now\n\n" + _CALL_PLACED_THEN_FAILED_TEXT
    assert texts == ["Ringing now", "\n\n" + _CALL_PLACED_THEN_FAILED_TEXT]
    assert done == saved == expected


@pytest.mark.asyncio
async def test_the_placed_call_text_follows_blank_streamed_text_without_a_separator(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="  "), _BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    texts, done, saved = await _placed_call_reply_chunks(agent, session_manager)

    assert texts == ["  ", _CALL_PLACED_THEN_FAILED_TEXT]
    assert done == saved == "  " + _CALL_PLACED_THEN_FAILED_TEXT


@pytest.mark.asyncio
async def test_the_placed_call_text_alone_when_nothing_was_streamed(settings, mock_llm, session_manager, cost_tracker):
    mock_llm.complete.side_effect = _dials_then_a_blank_reply()
    mock_llm.stream_turn = _stream_of(_BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    texts, done, saved = await _placed_call_reply_chunks(agent, session_manager)

    assert texts == [_CALL_PLACED_THEN_FAILED_TEXT]
    assert done == saved == _CALL_PLACED_THEN_FAILED_TEXT


def _stream_that_fails_after(*texts: str):
    async def stream_turn(**kwargs):
        for text in texts:
            yield StreamTurnEvent(text=text)
        raise RuntimeError("connection reset")

    return stream_turn


def _tool_loop_reply(content: str) -> LLMResponse:
    return LLMResponse(content=content, model="test", input_tokens=80, output_tokens=5, stop_reason="end_turn")


async def _final_stream_log(agent, caplog) -> tuple[str, str]:
    """(the DONE text, everything the agent logged) of one streamed turn."""
    with caplog.at_level(logging.INFO, logger="pincer.core.agent"):
        chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]
    return chunks[-1].content, caplog.text


@pytest.mark.asyncio
async def test_a_failed_final_stream_logs_that_it_kept_the_streamed_text(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    mock_llm.complete.side_effect = [_tool_loop_reply("Hello there")]
    mock_llm.stream_turn = _stream_that_fails_after("Hel")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    done, log = await _final_stream_log(agent, caplog)

    assert done == "Hel"  # the precondition: the partial text is the reply
    assert "Final stream failed after 3 chars" in log
    assert "Keeping the 3 streamed chars" in log
    assert "tool-loop" not in log  # the reply did not come from the tool loop


@pytest.mark.asyncio
async def test_a_failed_final_stream_logs_that_it_used_the_tool_loop_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    mock_llm.complete.side_effect = [_tool_loop_reply("Hello there")]
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    done, log = await _final_stream_log(agent, caplog)

    assert done == "Hello there"
    assert "Final stream failed after 0 chars" in log
    assert "Using the tool-loop response" in log
    assert "Keeping" not in log


@pytest.mark.asyncio
async def test_a_failed_final_stream_logs_that_it_had_no_fallback(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    _stream_turn_fails_before_its_first_event(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with caplog.at_level(logging.INFO, logger="pincer.core.agent"), pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]

    assert "Final stream failed after 0 chars" in caplog.text
    assert "No usable fallback" in caplog.text
    assert "Using the tool-loop response" not in caplog.text


class _SdkError(Exception):
    """Stands in for a raw SDK or httpx error that is not an LLMError."""


_NON_LLM_ERRORS = [
    pytest.param(lambda: _SdkError("socket closed"), id="sdk-error"),
    pytest.param(lambda: TypeError("unexpected None"), id="type-error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", _NON_LLM_ERRORS)
async def test_a_streamed_turn_that_raises_a_non_llm_error_after_a_dial_still_returns_the_placed_call(
    settings, mock_llm, session_manager, cost_tracker, make_error
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], make_error()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", _NON_LLM_ERRORS)
async def test_a_turn_that_raises_a_non_llm_error_after_a_dial_still_returns_the_placed_call(
    settings, mock_llm, session_manager, cost_tracker, make_error
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], make_error()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    result = await agent.handle_message("user1", "test", "call them", memory=False)

    assert result.call_sids == ["CA9"]
    assert result.text == _CALL_PLACED_THEN_FAILED_TEXT
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[-1].role == MessageRole.ASSISTANT
    assert session.messages[-1].content == _CALL_PLACED_THEN_FAILED_TEXT


async def _run_streamed(agent) -> None:
    _chunks = [c async for c in agent.handle_message_stream("user1", "test", "call them", memory=False)]


async def _run_blocking(agent) -> None:
    await agent.handle_message("user1", "test", "call them", memory=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_streamed, _run_blocking], ids=["stream", "blocking"])
@pytest.mark.parametrize("make_error", _NON_LLM_ERRORS)
async def test_a_non_llm_error_with_no_call_placed_is_re_raised_unchanged(
    settings, mock_llm, session_manager, cost_tracker, make_error, run
):
    """The very same exception object comes out: not wrapped, not converted to an LLMError."""
    boom = make_error()
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], boom]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry([_DIAL_RESULTS[0]] * 2))

    with pytest.raises(type(boom)) as raised:
        await run(agent)

    assert raised.value is boom


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_streamed, _run_blocking], ids=["stream", "blocking"])
async def test_a_non_llm_error_after_a_dial_is_logged_with_its_traceback(
    settings, mock_llm, session_manager, cost_tracker, run, caplog
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _SdkError("socket closed")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"):
        await run(agent)

    [record] = [r for r in caplog.records if "placed" in r.getMessage()]
    assert record.exc_info is not None
    assert record.exc_info[0] is _SdkError


@pytest.mark.asyncio
async def test_a_budget_stop_after_a_dial_keeps_its_own_reply(settings, mock_llm, session_manager, tmp_path):
    """The dialling request crosses the budget ($7/MTok x 1,000 = the whole $0.007): the turn
    stops before its next request with the budget reply, never the placed-call text."""
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    dial = _two_dials_then_text()[0]
    dial.request_model, dial.input_tokens, dial.output_tokens = "test-fixture-model", 1_000, 0
    mock_llm.complete.side_effect = [dial, _two_dials_then_text()[1]]
    tracker = CostTracker(tmp_path / "test.db", daily_budget=0.007, prices=load_price_table())
    await tracker.initialize()
    agent = Agent(
        settings.model_copy(update={"daily_budget_usd": 0.007}),
        mock_llm,
        session_manager,
        tracker,
        _dialing_registry(_DIAL_RESULTS),
    )

    try:
        result = await agent.handle_message("user1", "test", "call them", memory=False)
    finally:
        await tracker.close()

    assert mock_llm.complete.await_count == 1
    assert result.text.startswith("Warning: Daily budget limit reached.")
    assert result.call_sids == ["CA9"]


@pytest.mark.asyncio
async def test_a_cancelled_streamed_turn_after_a_dial_is_not_swallowed(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], asyncio.CancelledError()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    with pytest.raises(asyncio.CancelledError):
        await _run_streamed(agent)


async def _final_stream_reply(agent) -> tuple[list[str], str]:
    """(the TEXT chunks, the DONE text) of one streamed turn, after checking that DONE == join(TEXT) == saved."""
    chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]
    assert chunks[-1].type == StreamEventType.DONE
    texts = [c.content for c in chunks if c.type == StreamEventType.TEXT]
    session = await agent._sessions.get_or_create("user1", "test")
    assert session.messages[-1].role == MessageRole.ASSISTANT
    assert chunks[-1].content == "".join(texts) == session.messages[-1].content
    return texts, chunks[-1].content


def _completed(content: str, stop_reason: str = "end_turn") -> StreamTurnEvent:
    return StreamTurnEvent(
        response=LLMResponse(content=content, model="test", input_tokens=80, output_tokens=5, stop_reason=stop_reason)
    )


def _streams_with_no_text() -> list:
    """The ways a final stream can end with nothing to say, each paired with its reason in the log."""
    return [
        pytest.param(_stream_of(_BLANK_FINAL), id="completed-blank"),
        pytest.param(_stream_of(), id="no-final-response"),
        pytest.param(_stream_of(StreamTurnEvent(text="  \n")), id="no-final-response-whitespace"),
        pytest.param(_stream_that_fails_after(), id="failed-before-any-text"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_turn", _streams_with_no_text())
async def test_a_final_stream_with_no_text_uses_the_tool_loop_text_as_a_chunk(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, stream_turn
):
    """The tool loop's last response already is a complete answer (comments 4494473533 and 4494473550)."""
    mock_llm.complete.side_effect = [_tool_loop_reply("Tool text")]
    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    texts, done = await _final_stream_reply(agent)

    assert texts[-1] == "Tool text"  # sent as a chunk, not only in DONE
    assert done.endswith("Tool text")
    assert done.strip() == "Tool text"  # a whitespace chunk already sent may precede it


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_turn", _streams_with_no_text())
async def test_a_final_stream_with_no_text_and_a_blank_tool_loop_response_is_an_error(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, stream_turn, caplog
):
    mock_llm.complete.side_effect = [_tool_loop_reply("")]
    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"), pytest.raises(LLMError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]

    assert "No usable fallback" in caplog.text
    assert "tool-loop has no text" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_turn", _streams_with_no_text())
async def test_a_final_stream_with_no_text_never_falls_back_to_a_cut_off_tool_loop_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, stream_turn
):
    mock_llm.complete.side_effect = [_cut_off_before_any_text()]  # a blank cut-off, let through by the tool loop
    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [_cut_off_before_any_text(), _cut_off_before_any_text("")], ids=["a", "b"])
async def test_a_cut_off_final_stream_raises_even_when_the_tool_loop_has_a_complete_answer(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, final
):
    """A cut-off final stream is not 'no text': it keeps raising, with no fallback."""
    mock_llm.complete.side_effect = [_tool_loop_reply("Tool text")]
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(response=final))
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMOutputTruncatedError):
        _chunks = [c async for c in agent.handle_message_stream("user1", "test", "hi", memory=False)]


@pytest.mark.asyncio
async def test_a_cut_off_final_stream_with_text_is_delivered(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = [_tool_loop_reply("Tool text")]
    mock_llm.stream_turn = _stream_of(StreamTurnEvent(text="Hel"), _completed("Hel", "max_tokens"))
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    texts, done = await _final_stream_reply(agent)

    assert (texts, done) == (["Hel"], "Hel")


@pytest.mark.asyncio
async def test_streamed_text_is_the_reply_when_the_stream_fails_or_closes_without_a_final_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    for stream_turn in (_stream_that_fails_after("Hel"), _stream_of(StreamTurnEvent(text="Hel"))):
        mock_llm.complete.side_effect = [_tool_loop_reply("Tool text")]
        mock_llm.stream_turn = stream_turn
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

        texts, done = await _final_stream_reply(agent)

        assert (texts, done) == (["Hel"], "Hel")  # never replaced by the tool-loop text
        session = await session_manager.get_or_create("user1", "test")
        session.messages.clear()


@pytest.mark.asyncio
async def test_a_whitespace_partial_is_kept_and_followed_by_the_tool_loop_text(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The whitespace was already sent as a chunk, so it stays: DONE == join(TEXT) == saved."""
    mock_llm.complete.side_effect = [_tool_loop_reply("Hello there")]
    mock_llm.stream_turn = _stream_that_fails_after("  ")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    texts, done = await _final_stream_reply(agent)

    assert texts == ["  ", "Hello there"]
    assert done == "  Hello there"


@pytest.mark.asyncio
async def test_a_completed_blank_stream_after_a_dial_with_a_cut_off_tool_loop_response_gets_the_notice(
    settings, mock_llm, session_manager, cost_tracker
):
    dials, _ringing = _two_dials_then_text()
    mock_llm.complete.side_effect = [dials, _cut_off_before_any_text()]
    mock_llm.stream_turn = _stream_of(_BLANK_FINAL)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    await _assert_the_placed_call_text_is_the_reply(agent, session_manager)


@pytest.mark.asyncio
async def test_a_turn_cut_off_before_any_text_after_placing_a_call_still_returns_it(
    settings, mock_llm, session_manager, cost_tracker
):
    mock_llm.complete.side_effect = [_two_dials_then_text()[0], _cut_off_before_any_text()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    result = await agent.handle_message("user1", "test", "call them")

    assert result.call_sids == ["CA9"]
    assert "placed the call" in result.text


@pytest.mark.asyncio
async def test_a_streamed_dial_done_carries_the_placed_call_sid(settings, mock_llm, session_manager, cost_tracker):
    mock_llm.complete.side_effect = _two_dials_then_text()
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _dialing_registry(_DIAL_RESULTS))

    done = [
        chunk.call_sid
        async for chunk in agent.handle_message_stream("user1", "test", "call them")
        if chunk.type == StreamEventType.TOOL_DONE
    ]

    assert done == ["", "CA9"]


@pytest.mark.asyncio
async def test_stream_tool_context_prefers_channel_user_id(settings, mock_llm, session_manager, cost_tracker):
    """Same fix applied to the streaming path (handle_message_stream previously dropped channel_user_id)."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    async for _chunk in agent.handle_message_stream(
        user_id="canonical-r_lutsiv",
        channel="test",
        text="who am i",
        channel_user_id="123456789",
    ):
        pass

    assert _without_turn_id(seen_contexts) == [
        {
            "user_id": "123456789",
            "channel": "test",
            "pincer_user_id": "canonical-r_lutsiv",
            "channel_name": "test",
            "timezone": "",
            "tool_call_id": "tc1",
            "session_key": "test:canonical-r_lutsiv",
        }
    ]


@pytest.mark.asyncio
async def test_run_headless_no_session_writes(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    """run_headless must not touch SessionManager at all (proactive/scheduled runs are session-free)."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with (
        patch.object(session_manager, "get_or_create", wraps=session_manager.get_or_create) as get_or_create_spy,
        patch.object(session_manager, "add_message", wraps=session_manager.add_message) as add_message_spy,
    ):
        result = await agent.run_headless("Say hi", user_id="usr1", channel="scheduled")

    assert get_or_create_spy.await_count == 0
    assert add_message_spy.await_count == 0
    assert "Hello! I'm Pincer." in result


@pytest.mark.asyncio
async def test_run_headless_tool_call_round_trip(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="The greeting is: Hello, World!",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.run_headless("Greet the world", user_id="usr1", channel="scheduled")

    assert "Hello, World!" in result
    assert mock_llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_run_headless_skips_approval(settings, mock_llm, session_manager, cost_tracker):
    """Tool calls in run_headless run with skip_approval=True — no live user to approve anything."""
    registry = ToolRegistry()

    async def risky() -> str:
        return "done"

    registry.register(name="risky", description="risky", handler=risky, require_approval=True)

    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="risky", arguments={})],
            model="test",
            input_tokens=10,
            output_tokens=5,
            stop_reason="tool_use",
        ),
        LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn"),
    ]

    approval_callback = AsyncMock(return_value=True)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry, approval_callback=approval_callback)

    result = await agent.run_headless("do the risky thing", user_id="usr1", channel="scheduled")

    approval_callback.assert_not_awaited()
    assert "done" in result


@pytest.mark.asyncio
async def test_run_headless_allowed_tools_filters_schema(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """allowed_tools restricts which tool schemas reach the LLM, not just which calls are permitted."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await agent.run_headless("hi", user_id="usr1", channel="scheduled", allowed_tools=["nonexistent_tool"])

    call_kwargs = mock_llm.complete.call_args.kwargs
    assert call_kwargs["tools"] is None


@pytest.mark.asyncio
async def test_run_headless_allowed_tools_keeps_matching_tool(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await agent.run_headless("hi", user_id="usr1", channel="scheduled", allowed_tools=["greet"])

    call_kwargs = mock_llm.complete.call_args.kwargs
    assert [t["name"] for t in call_kwargs["tools"]] == ["greet"]


@pytest.mark.asyncio
async def test_run_headless_llm_error(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = LLMError("connection reset")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="usr1", channel="scheduled")

    assert "llm error" in result.lower()


@pytest.mark.asyncio
async def test_run_headless_circuit_breaker_after_repeated_tool_errors(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A tool call that keeps failing (unknown tool name) trips the circuit breaker after 3 iterations."""
    mock_llm.complete.return_value = LLMResponse(
        content="still trying",
        tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
        model="test",
        input_tokens=10,
        output_tokens=5,
        stop_reason="tool_use",
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("do the thing", user_id="usr1", channel="scheduled")

    assert result == "still trying"
    assert mock_llm.complete.call_count == 3


@pytest.mark.asyncio
async def test_run_headless_exhausts_iterations_returns_last_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """With max_iterations reached and no final no-tool-call response, the last response content is returned."""
    mock_llm.complete.return_value = LLMResponse(
        content="partial thoughts",
        tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
        model="test",
        input_tokens=10,
        output_tokens=5,
        stop_reason="tool_use",
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="usr1", channel="scheduled", max_iterations=1)

    assert result == "partial thoughts"
    assert mock_llm.complete.call_count == 1


@pytest.mark.asyncio
async def test_execute_tool_sandbox_denied_logs_warning_not_error(
    settings, mock_llm, session_manager, cost_tracker, caplog, tmp_path, monkeypatch
):
    """An out-of-workspace file_read (issue #218) is expected policy, not a crash — no ERROR, no traceback.

    Uses the real file_read so the whole file_read → _sandbox_path → confine_path chain is exercised.
    """
    monkeypatch.setattr(files, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    registry = ToolRegistry()
    registry.register(
        name="file_read",
        description="Read a file",
        handler=files.file_read,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    tool_call = ToolCall(id="tc1", name="file_read", arguments={"path": "/etc/passwd"})

    with caplog.at_level(logging.INFO):
        result = await agent._execute_tool(tool_call, "user1", "test")

    assert result.is_error is True
    assert "outside workspace" in result.content
    assert "All file operations are sandboxed." in result.content

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is None


@pytest.mark.asyncio
async def test_execute_tool_genuine_error_still_logs_error_with_traceback(
    settings, mock_llm, session_manager, cost_tracker, caplog
):
    """Non-sandbox tool failures are unaffected — still ERROR level, with a traceback."""
    registry = ToolRegistry()

    async def boom() -> str:
        raise RuntimeError("something actually broke")

    registry.register(
        name="boom",
        description="Always fails",
        handler=boom,
        parameters={"type": "object", "properties": {}},
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    tool_call = ToolCall(id="tc1", name="boom", arguments={})

    with caplog.at_level(logging.INFO):
        result = await agent._execute_tool(tool_call, "user1", "test")

    assert result.is_error is True
    assert "RuntimeError" in result.content

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None


async def test_unavailable_memories_warn_and_still_reply(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """A failing memory backend costs the user their memories, not their reply — and says so at WARNING."""
    memory = AsyncMock()
    memory.search_text.side_effect = ConnectionError("memory backend down")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        result = await agent.handle_message("user1", "test", "Hello!")

    assert "Hello! I'm Pincer." in result.text
    memory.search_text.assert_awaited()
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert MEMORY_BLOCK_HEADER not in system_prompt

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage() == "Failed to fetch memories for prompt"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert warnings[0].exc_info[0] is ConnectionError


def _outage_log(caplog: pytest.LogCaptureFixture, failure: str, recovery: str) -> list[tuple[str, bool]]:
    """(level, has traceback) of each failure/recovery record, in order."""
    return [
        (record.levelname, record.exc_info is not None)
        for record in caplog.records
        if record.getMessage() in {failure, recovery}
    ]


async def test_an_unavailable_memory_backend_warns_once_per_outage(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """Once per message would flood the log for the length of an outage."""
    memory = AsyncMock()
    memory.search_text.side_effect = [
        ConnectionError("down"),
        ConnectionError("still down"),
        [],
        ConnectionError("again"),
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(4):
            await agent.handle_message("user1", "test", "Hello!")

    assert memory.search_text.await_count == 4
    assert _outage_log(caplog, "Failed to fetch memories for prompt", "Fetching memories for prompt recovered") == [
        ("WARNING", True),
        ("DEBUG", True),
        ("INFO", False),
        ("WARNING", True),
    ]


async def test_an_unavailable_mcp_server_list_warns_once_per_outage(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    agent.mcp_manager = AsyncMock()
    agent.mcp_manager.list_servers.side_effect = [
        RuntimeError("down"),
        RuntimeError("still down"),
        [],
        RuntimeError("again"),
    ]

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(4):
            await agent.handle_message("user1", "test", "Hello!")

    assert agent.mcp_manager.list_servers.await_count == 4
    assert _outage_log(
        caplog, "Failed to fetch MCP server list for prompt", "Fetching the MCP server list for prompt recovered"
    ) == [("WARNING", True), ("DEBUG", True), ("INFO", False), ("WARNING", True)]


class _Unprintable:
    @property
    def content(self) -> str:
        raise RuntimeError("cannot render")


async def test_a_memory_that_fails_to_render_warns_once_and_never_recovers(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """The fetch succeeds, then formatting it fails: that is one outage, not a
    recovery followed by a new failure on every message."""
    memory = AsyncMock()
    memory.search_text.return_value = [_Unprintable()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(3):
            await agent.handle_message("user1", "test", "Hello!")

    assert _outage_log(caplog, "Failed to fetch memories for prompt", "Fetching memories for prompt recovered") == [
        ("WARNING", True),
        ("DEBUG", True),
        ("DEBUG", True),
    ]


async def test_an_mcp_server_list_that_fails_to_render_warns_once_and_never_recovers(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    agent.mcp_manager = AsyncMock()
    agent.mcp_manager.list_servers.return_value = [{"connected": True}]  # no "name": formatting raises

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(3):
            await agent.handle_message("user1", "test", "Hello!")

    assert _outage_log(
        caplog, "Failed to fetch MCP server list for prompt", "Fetching the MCP server list for prompt recovered"
    ) == [("WARNING", True), ("DEBUG", True), ("DEBUG", True)]


def _mcp_memory_failing_store() -> MCPMemoryBackend:
    """An MCP memory backend whose memory_store returns a fastmcp tool error."""

    async def call_tool(name: str, args: dict) -> MagicMock:
        if name == "memory_store":
            err = [MagicMock(text="Error calling tool 'memory_store': embedding failed")]
            return MagicMock(content=err, isError=True, structuredContent=None)
        return MagicMock(content=[], isError=False, structuredContent=None)

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend = MCPMemoryBackend(server_name="memory-test")
    backend.set_mcp_manager(manager)
    return backend


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_summarization_memory_error_does_not_fail_the_turn(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog, streaming
):
    """A memory_store tool error during summarization is logged; the user still gets a reply."""
    memory = _mcp_memory_failing_store()
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )

    async def turn(text: str) -> None:
        if not streaming:
            result = await agent.handle_message("user1", "test", text)
            assert "Hello! I'm Pincer." in result.text
            return
        chunks = [c async for c in agent.handle_message_stream(user_id="user1", channel="test", text=text)]
        assert chunks[-1].type == StreamEventType.DONE

    await turn("first")
    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"):
        await turn("second")  # 3 non-system messages >= threshold → summarize

    warnings = [r for r in caplog.records if r.getMessage() == "Session summarization failed"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def _unpaired_call_ids(messages: list[LLMMessage]) -> set[str]:
    """IDs of assistant tool calls with no matching tool result."""
    result_ids = {m.tool_call_id for m in messages if m.role == MessageRole.TOOL_RESULT}
    return {tc.id for m in messages if m.role == MessageRole.ASSISTANT for tc in m.tool_calls} - result_ids


def _recording_complete(
    sent: list[list[LLMMessage]], *responses: LLMResponse | Exception
) -> Callable[..., Awaitable[LLMResponse]]:
    """A complete() side effect that snapshots the history each call was sent."""
    queue = list(responses)

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        sent.append(list(messages))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return complete


async def _run_turn(agent: Agent, text: str, *, streaming: bool) -> str:
    """One turn through handle_message or handle_message_stream; returns the reply."""
    if not streaming:
        return (await agent.handle_message("user1", "test", text)).text
    chunks = [c async for c in agent.handle_message_stream(user_id="user1", channel="test", text=text)]
    return "".join(c.content for c in chunks if c.type == StreamEventType.DONE)


def _orphaned_call(call_id: str = "late") -> LLMMessage:
    return LLMMessage(
        role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id=call_id, name="greet", arguments={})]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "tool_calls"),
    [
        # One call answered, one not: the message is rewritten, not dropped.
        ("", [ToolCall(id="ok", name="greet", arguments={}), ToolCall(id="orphan", name="greet", arguments={})]),
        # Every call orphaned, but the text survives as a plain message.
        ("Let me check.", [ToolCall(id="orphan", name="greet", arguments={})]),
    ],
    ids=["partially-orphaned", "orphaned-with-text"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_orphaned_tool_calls_never_reach_the_llm(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, content, tool_calls, streaming
):
    """Sanitizing that rewrites a message keeps the history's length, which
    used to hide the repair — the orphaned calls were sent and OpenAI 400'd."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls),
    ]
    if any(tc.id == "ok" for tc in tool_calls):
        session.messages.append(LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"))

    sent: list[list[LLMMessage]] = []
    mock_llm.complete.side_effect = _recording_complete(sent, _DONE)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _run_turn(agent, "again", streaming=streaming)

    assert len(sent) == 1
    assert _unpaired_call_ids(sent[0]) == set()
    assert _unpaired_call_ids(session.messages) == set()


@pytest.mark.asyncio
async def test_repaired_history_is_persisted(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path
):
    """The repair is saved, not just applied to the cached session: the
    turn fails before anything else would write the session out."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [LLMMessage(role=MessageRole.USER, content="hi"), _orphaned_call()]
    mock_llm.complete.side_effect = LLMError("openai API error: 500 server error")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "again")

    # A second manager on the same database reads the stored row, not the cache.
    store = SessionManager(Path(tmp_path) / "test.db")
    await store.initialize()
    try:
        stored = await store.get_or_create("user1", "test")
        assert [m.content for m in stored.messages] == ["hi", "again"]
        assert _unpaired_call_ids(stored.messages) == set()
    finally:
        await store.close()


def test_sanitize_requires_results_directly_after_their_call():
    """OpenAI rejects a result that isn't in the run right after its call,
    so a result separated by another message counts as orphaned, as does a
    second result for the same call."""
    messages = [
        _orphaned_call(),
        LLMMessage(role=MessageRole.USER, content="interrupting"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="late"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="ok", name="greet", arguments={})]),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello again!", tool_call_id="ok"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [(m.role, m.tool_call_id) for m in clean] == [
        (MessageRole.USER, None),
        (MessageRole.ASSISTANT, None),
        (MessageRole.TOOL_RESULT, "ok"),
    ]
    assert clean[2].content == "Hello!"


def test_sanitize_pairs_a_repeated_call_id_once():
    """A compatible server can return the same (or an empty) id for two calls;
    only the first call and its first result survive, so the output is clean."""
    messages = [
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="x", name="greet", arguments={}), ToolCall(id="x", name="greet", arguments={})],
        ),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="ra", tool_call_id="x"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="rb", tool_call_id="x"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [tc.id for tc in clean[0].tool_calls] == ["x"]
    assert [m.content for m in clean[1:]] == ["ra"]
    assert _sanitize_tool_pairs(clean) is clean


def test_sanitize_returns_a_clean_history_unchanged():
    messages = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="ok", name="greet", arguments={})]),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"),
    ]
    assert _sanitize_tool_pairs(messages) is messages


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_OPENAI_UNPAIRED, True),
        (
            "openai API error: 400 Invalid parameter: messages with role 'tool' must be a response to a "
            "preceeding message with 'tool_calls'.",
            True,
        ),
        (_ANTHROPIC_UNPAIRED, True),
        (_ANTHROPIC_NO_TOOLS, True),
        ("openai API error: 400 Invalid 'messages[2].tool_calls[0].id': string too long", False),
        ("openai API error: 400 Invalid 'tool_call_id': expected a string", False),
    ],
    ids=[
        "openai-unanswered-call",
        "openai-stray-result",
        "anthropic-unanswered-call",
        "anthropic-no-tools",
        "openai-bad-id",
        "openai-bad-tool-call-id",
    ],
)
def test_is_tool_pair_error(error, expected):
    assert _is_tool_pair_error(LLMError(error)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_tool_pair_error_sanitizes_and_retries(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """A history that turns bad between the proactive pass and the call (a
    concurrent turn writing to the session) is repaired once the provider
    rejects it, and the retry no longer carries the orphan."""
    sent: list[list[LLMMessage]] = []

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        sent.append(list(messages))
        if len(sent) == 1:
            messages.append(_orphaned_call())
            raise LLMError(_OPENAI_UNPAIRED)
        return _DONE

    mock_llm.complete.side_effect = complete
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await _run_turn(agent, "hi", streaming=streaming)

    assert len(sent) == 2
    assert _unpaired_call_ids(sent[1]) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [_OPENAI_UNPAIRED, _ANTHROPIC_UNPAIRED, _ANTHROPIC_NO_TOOLS],
    ids=["openai-unanswered-call", "anthropic-unanswered-call", "anthropic-no-tools"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_unrepairable_tool_pair_error_clears_the_session_without_retrying(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, error, streaming
):
    """When stripping changes nothing, re-sending would fail the same way on
    this and every later turn (e.g. Anthropic's "must define tools" for a
    history with tool pairs sent with no tools), so the session is cleared."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="earlier"),
        LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"),
    ]
    mock_llm.complete.side_effect = LLMError(error)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    reply = await _run_turn(agent, "hi", streaming=streaming)

    assert reply.startswith(_SESSION_CLEARED_TEXT)
    assert mock_llm.complete.call_count == 1
    assert "earlier" not in [m.content for m in session.messages]


@pytest.mark.asyncio
async def test_other_llm_errors_raise_and_keep_the_history(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="earlier"),
        LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"),
    ]
    mock_llm.complete.side_effect = LLMError("openai API error: 400 Invalid 'tool_call_id': expected a string")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "hi")

    assert mock_llm.complete.call_count == 1
    assert [m.content for m in session.messages] == ["earlier", "earlier reply", "hi"]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_turn_without_memory_neither_reads_nor_writes_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """memory=False (a call summary, a knowledge-base question): the user's
    long-term memory stays out of the prompt, and nothing of the turn is
    stored there, not even as a session summary."""
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="the user's private fact")]
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    summarizer.maybe_summarize = AsyncMock(wraps=summarizer.maybe_summarize)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )

    for text in ("summarize call one", "summarize call two"):
        if streaming:
            chunks = [c async for c in agent.handle_message_stream("user1", "test", text, memory=False)]
            assert chunks[-1].type == StreamEventType.DONE
        else:
            result = await agent.handle_message("user1", "test", text, memory=False)
            assert "Hello! I'm Pincer." in result.text

    memory.search_text.assert_not_awaited()
    memory.store_memory.assert_not_awaited()
    memory.add_profile.assert_not_awaited()
    summarizer.maybe_summarize.assert_not_awaited()
    for call in mock_llm.complete.call_args_list:
        assert "the user's private fact" not in call.kwargs["system"]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_conversation_without_memory_stays_without_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """A memory-free conversation continued with memory=True is still
    memory-free: its exchanges never reach long-term memory, summary included."""
    memory = AsyncMock()
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    summarizer.maybe_summarize = AsyncMock(wraps=summarizer.maybe_summarize)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )
    key = "test:user1:summary"

    for text, with_memory in (("summarize the call", False), ("and the action items?", True)):
        if streaming:
            stream = agent.handle_message_stream("user1", "test", text, session_key=key, memory=with_memory)
            chunks = [c async for c in stream]
            assert chunks[-1].type == StreamEventType.DONE
        else:
            await agent.handle_message("user1", "test", text, session_key=key, memory=with_memory)

    memory.search_text.assert_not_awaited()
    memory.store_memory.assert_not_awaited()
    summarizer.maybe_summarize.assert_not_awaited()


async def test_a_turn_with_memory_still_reads_and_writes_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The default is unchanged: memory is searched and the exchange stored."""
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="the user's private fact")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "hello")

    memory.search_text.assert_awaited()
    memory.store_memory.assert_awaited_once()
    assert "the user's private fact" in mock_llm.complete.call_args.kwargs["system"]


async def test_memory_block_is_labelled_unverified(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    """Recalled memories are marked as conversation text, not facts checked against Contacts (#343)."""
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="Jane Doe is a friend")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "who is Jane?")

    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert f"{MEMORY_BLOCK_HEADER}\n- Jane Doe is a friend" in system_prompt
    assert "not verified against Contacts" in MEMORY_BLOCK_HEADER


class _RecordingSelector(ToolSelector):
    """A ToolSelector that keeps the names in ``keeps`` (one set per call, the last repeats) and
    records each call's query and recent tools."""

    def __init__(self, settings: Settings, *keeps: set[str]) -> None:
        super().__init__(None, settings)
        self.keeps = list(keeps)
        self.calls: list[tuple[str, list[str]]] = []

    async def select(
        self, schemas: list[dict[str, Any]], query: str, recent_tool_names: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        self.calls.append((query, list(recent_tool_names)))
        keep = self.keeps[min(len(self.calls), len(self.keeps)) - 1]
        return [s for s in schemas if s["name"] in keep]


def _phone_and_google_registry(skill_text: str = "skill") -> ToolRegistry:
    registry = ToolRegistry()

    async def noop() -> str:
        return "ok"

    async def load_skill(name: str = "") -> str:
        return skill_text

    for name in ("make_phone_call", "google__search_drive_files", "whoami"):
        registry.register(name=name, description=name, handler=noop, parameters={"type": "object", "properties": {}})
    registry.register(
        name="load_skill", description="load_skill", handler=load_skill, parameters={"type": "object", "properties": {}}
    )
    return registry


def _tool_names(call: Any) -> list[str]:
    return [t["name"] for t in call.kwargs["tools"]]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_tool_selector_sets_tools_and_prompt_hints(settings, mock_llm, session_manager, cost_tracker, streaming):
    """The selected tools are what the LLM gets, and the phone/google hints follow them, not the registry."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry())
    selector = _RecordingSelector(settings, {"whoami"})
    agent.tool_selector = selector

    session = await session_manager.get_or_create("user1", "test")
    session.messages.append(
        LLMMessage(
            role=MessageRole.ASSISTANT, tool_calls=[ToolCall(id="t0", name="google__search_drive_files", arguments={})]
        )
    )

    if streaming:
        async for _chunk in agent.handle_message_stream(user_id="user1", channel="test", text="Ruf mich an"):
            pass
    else:
        await agent.handle_message("user1", "test", "Ruf mich an")

    assert selector.calls == [("Ruf mich an", ["google__search_drive_files"])]
    kwargs = mock_llm.complete.call_args.kwargs
    assert _tool_names(mock_llm.complete.call_args) == ["whoami"]
    assert "make_phone_call tool" not in kwargs["system"]
    assert "google__" not in kwargs["system"]


def _registry_with(*names: str) -> ToolRegistry:
    registry = ToolRegistry()

    async def noop() -> str:
        return "ok"

    for name in names:
        registry.register(name=name, description=name, handler=noop, parameters={"type": "object", "properties": {}})
    return registry


@pytest.mark.parametrize("tool", ["google__search_contacts", "google__list_contacts", "google__get_contact"])
async def test_contacts_tools_get_contacts_hint(settings, mock_llm, session_manager, cost_tracker, tool):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _registry_with(tool))
    await agent.handle_message("user1", "test", "find Jane")
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "Google Contacts — always verify and name the source" in system_prompt
    assert "first name alone and the last name alone" in system_prompt


async def test_phone_call_hint_names_available_lookups(settings, mock_llm, session_manager, cost_tracker):
    agent = Agent(
        settings,
        mock_llm,
        session_manager,
        cost_tracker,
        _registry_with("make_phone_call", "google__search_contacts", "contact_lookup"),
    )
    await agent.handle_message("user1", "test", "call Jane")
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert (
        "look the number up first with google__search_contacts (Google Contacts),"
        " then contact_lookup (saved phone contacts), then call."
    ) in system_prompt


async def test_phone_call_hint_without_lookup_tools(settings, mock_llm, session_manager, cost_tracker):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _registry_with("make_phone_call"))
    await agent.handle_message("user1", "test", "call Jane")
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "make_phone_call tool" in system_prompt
    assert "look the number up" not in system_prompt


async def test_without_selector_prompt_hints_follow_registry(settings, mock_llm, session_manager, cost_tracker):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry())
    await agent.handle_message("user1", "test", "hi")
    kwargs = mock_llm.complete.call_args.kwargs
    assert len(kwargs["tools"]) == 4
    assert "make_phone_call tool" in kwargs["system"]


async def test_selection_query_adds_the_assistant_message_being_answered(
    settings, mock_llm, session_manager, cost_tracker
):
    """A short reply ("ja, bitte") ranks on the assistant's proposal it answers, too."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry())
    selector = _RecordingSelector(settings, {"whoami"})
    agent.tool_selector = selector
    session = await session_manager.get_or_create("user1", "test")
    session.messages.append(LLMMessage(role=MessageRole.ASSISTANT, content="Soll ich die Zahnarztpraxis anrufen?"))

    await agent.handle_message("user1", "test", "ja, bitte")

    assert selector.calls[0][0] == "ja, bitte\nSoll ich die Zahnarztpraxis anrufen?"


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_loading_a_skill_reselects_tools_mid_turn(settings, mock_llm, session_manager, cost_tracker, streaming):
    """A skill loaded mid-turn can reach tools the user's message didn't rank."""
    skill = "Budget skill: find the sheet with google__search_drive_files"
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry(skill))
    selector = _RecordingSelector(settings, {"whoami", "load_skill"}, {"load_skill", "google__search_drive_files"})
    agent.tool_selector = selector
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="load_skill", arguments={"name": "budget"})],
            model="test",
            input_tokens=10,
            output_tokens=5,
            stop_reason="tool_use",
        ),
        LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn"),
    ]

    if streaming:
        async for _chunk in agent.handle_message_stream(user_id="user1", channel="test", text="Budget?"):
            pass
    else:
        await agent.handle_message("user1", "test", "Budget?")

    assert [q for q, _ in selector.calls] == ["Budget?", f"{skill}\nBudget?"]
    assert selector.calls[1][1][0] == "load_skill"  # this turn's calls are recent tools, so they stay
    first, second = mock_llm.complete.call_args_list
    assert set(_tool_names(first)) == {"load_skill", "whoami"}
    assert set(_tool_names(second)) == {"load_skill", "google__search_drive_files"}
    # The system prompt is rebuilt with the new set: its google__ hints follow the tools sent.
    assert "google__" not in first.kwargs["system"]
    assert "google__" in second.kwargs["system"]


def _load_skill_then_answer() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="load_skill", arguments={"name": "budget"})],
            model="test",
            input_tokens=10,
            output_tokens=5,
            stop_reason="tool_use",
        ),
        LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn"),
    ]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_failed_skill_load_keeps_the_turns_tools(settings, mock_llm, session_manager, cost_tracker, streaming):
    """load_skill reports an unknown name as an "Error: ..." result, not is_error: no skill loaded, no re-selection."""
    failure = "Error: no skill named 'budget'. Available skills: a, b, c"
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry(failure))
    selector = _RecordingSelector(settings, {"whoami", "load_skill"}, {"google__search_drive_files"})
    agent.tool_selector = selector
    mock_llm.complete.side_effect = _load_skill_then_answer()

    if streaming:
        async for _chunk in agent.handle_message_stream(user_id="user1", channel="test", text="Budget?"):
            pass
    else:
        await agent.handle_message("user1", "test", "Budget?")

    assert [q for q, _ in selector.calls] == ["Budget?"]
    first, second = mock_llm.complete.call_args_list
    assert _tool_names(second) == _tool_names(first)
    assert second.kwargs["system"] == first.kwargs["system"]


async def test_run_headless_reselects_from_allowed_tools_after_a_skill(
    settings, mock_llm, session_manager, cost_tracker
):
    """Scheduled actions re-select after load_skill too, but only from their allowed_tools."""
    skill = "Budget skill: find the sheet with google__search_drive_files"
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry(skill))
    offered: list[set[str]] = []

    class _Selector(_RecordingSelector):
        async def select(self, schemas, query, recent_tool_names=()):
            offered.append({s["name"] for s in schemas})
            return await super().select(schemas, query, recent_tool_names)

    selector = _Selector(settings, {"load_skill"}, {"load_skill", "google__search_drive_files"})
    agent.tool_selector = selector
    mock_llm.complete.side_effect = _load_skill_then_answer()
    allowed = ["load_skill", "google__search_drive_files", "whoami"]

    await agent.run_headless("Run the budget skill", "user1", "scheduler", allowed_tools=allowed)

    assert offered == [set(allowed), set(allowed)]  # never make_phone_call, which isn't allowed
    assert selector.calls[1] == (f"{skill}\nRun the budget skill", ["load_skill"])
    first, second = mock_llm.complete.call_args_list
    assert _tool_names(first) == ["load_skill"]
    assert set(_tool_names(second)) == {"load_skill", "google__search_drive_files"}
    assert "google__" not in first.kwargs["system"]
    assert "google__" in second.kwargs["system"]


async def test_in_call_prompt_hints_follow_the_offered_tools(settings, mock_llm, session_manager, cost_tracker):
    """In a call the gate owns the tool set and the hints follow it (issue #300): a hint for a tool
    the model was not given gets the nearest offered tool called in its place."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry())
    agent.tool_selector = _RecordingSelector(settings, {"whoami"})
    gate = MagicMock()
    gate.filter_schemas.side_effect = lambda schemas: [s for s in schemas if s["name"] == "whoami"]

    with patch("pincer.voice.in_call_tools.current_gate", return_value=gate):
        await agent.handle_message("user1", "test", "Ruf mich an")

    kwargs = mock_llm.complete.call_args.kwargs
    assert _tool_names(mock_llm.complete.call_args) == ["whoami"]
    assert "make_phone_call tool" not in kwargs["system"]
    assert "Google Workspace Tool Chains" not in kwargs["system"]
    assert agent.tool_selector.calls == []  # the gate, not the selector, picks in-call tools


@pytest.mark.parametrize(
    ("allowed", "expected_offered"),
    [
        (None, {"make_phone_call", "google__search_drive_files", "whoami", "load_skill"}),
        (["whoami", "load_skill"], {"whoami", "load_skill"}),
    ],
    ids=["all-tools", "allowed_tools"],
)
async def test_run_headless_selects_from_the_allowed_tools(
    settings, mock_llm, session_manager, cost_tracker, allowed, expected_offered
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, _phone_and_google_registry())
    offered: list[set[str]] = []

    class _Selector(_RecordingSelector):
        async def select(self, schemas, query, recent_tool_names=()):
            offered.append({s["name"] for s in schemas})
            return await super().select(schemas, query, recent_tool_names)

    selector = _Selector(settings, {"whoami"})
    agent.tool_selector = selector

    await agent.run_headless("Check the budget", "user1", "scheduler", allowed_tools=allowed)

    assert offered == [expected_offered]
    assert selector.calls == [("Check the budget", [])]
    kwargs = mock_llm.complete.call_args.kwargs
    assert _tool_names(mock_llm.complete.call_args) == ["whoami"]
    assert "make_phone_call tool" not in kwargs["system"]


@pytest.mark.asyncio
async def test_tool_calls_share_one_turn_id_per_turn_and_zone_is_looked_up_once(
    settings, mock_llm, session_manager, cost_tracker
):
    registry, seen_contexts = _context_capturing_registry()

    def _two_calls_then_done():
        return [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="whoami", arguments={}),
                    ToolCall(id="tc2", name="whoami", arguments={}),
                ],
                model="test",
                input_tokens=50,
                output_tokens=30,
                stop_reason="tool_use",
            ),
            LLMResponse(content="done", model="test", input_tokens=80, output_tokens=20, stop_reason="end_turn"),
        ]

    mock_llm.complete.side_effect = [*_two_calls_then_done(), *_two_calls_then_done()]
    looked_up: list[str] = []

    class _Identity:
        async def get_timezone(self, pincer_user_id: str) -> str:
            looked_up.append(pincer_user_id)
            return "Europe/Kyiv"

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    agent.identity_resolver = _Identity()
    await agent.handle_message(user_id="u1", channel="test", text="first")
    lookups_after_first_turn = len(looked_up)
    await agent.handle_message(user_id="u1", channel="test", text="second")

    turn_ids = [ctx["turn_id"] for ctx in seen_contexts]
    assert [ctx["tool_call_id"] for ctx in seen_contexts] == ["tc1", "tc2", "tc1", "tc2"]
    assert turn_ids[0] == turn_ids[1]
    assert turn_ids[2] == turn_ids[3]
    assert turn_ids[0] != turn_ids[2]
    assert all(_UUID7_RE.match(t) for t in turn_ids)
    assert lookups_after_first_turn == 1
    assert len(looked_up) == 2


# ── #305: an explicit language request wins and persists ─────────────


async def test_system_prompt_lets_an_explicit_language_request_win(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await agent.handle_message("user1", "test", "Please speak Ukrainian from now on")

    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "explicitly asks for a language" in system_prompt
    assert "keep using it in every reply until they ask for another" in system_prompt
    assert "Never refuse a language you can write." in system_prompt
    assert "Always respond in the same language the user writes in" not in system_prompt


async def test_stored_preferred_language_reaches_the_prompt_without_a_search_hit(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The profile is read directly: a message sharing no word with it still gets its language."""
    memory = AsyncMock()
    memory.search_text.return_value = []
    memory.list_memories.return_value = [
        SimpleNamespace(id="m1", content="The user's name is Olena. Preferred language: uk.")
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "What's the weather tomorrow?")

    memory.list_memories.assert_awaited_with(user_id="user1", category="profile", limit=1)
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "[User preferred language: uk]" in system_prompt
    assert MEMORY_BLOCK_HEADER not in system_prompt


async def test_no_preferred_language_line_without_a_profile(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    memory = AsyncMock()
    memory.search_text.return_value = []
    memory.list_memories.return_value = []
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "hello")

    assert "[User preferred language:" not in mock_llm.complete.call_args.kwargs["system"]


async def test_voice_turns_leave_the_preferred_language_to_the_call(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A call speaks its pinned language; an outbound call's other party is not the user."""
    memory = AsyncMock()
    memory.search_text.return_value = []
    memory.list_memories.return_value = [SimpleNamespace(id="m1", content="Preferred language: uk.")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "voice", "Hallo, ich rufe wegen des Termins an.")

    assert "[User preferred language:" not in mock_llm.complete.call_args.kwargs["system"]


async def test_failed_language_read_keeps_the_memories(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="likes tea")]
    memory.list_memories.side_effect = ConnectionError("profile read failed")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "hello")

    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "likes tea" in system_prompt
    assert "[User preferred language:" not in system_prompt
