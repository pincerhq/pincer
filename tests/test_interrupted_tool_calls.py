"""A turn that dies mid-tool is closed by appending error results, never by deleting its history (#358)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, text, thinking, tool_use

from pincer.core.agent import Agent, StreamEventType
from pincer.core.session import SessionManager
from pincer.llm.base import LLMMessage, MessageRole, ToolCall
from pincer.tools.registry import ToolRegistry

_THOUGHT = "SECRET-THOUGHT-7f3a"
_SIGNATURE = "SIG-7f3a-signature"
_INTERRUPTED = "(interrupted: the tool did not finish)"
_TOOL_CALL = [thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_1", "create_event", {"title": "Dentist"})]
_TOOL_CALL_BLOCKS = [
    {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
    {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {"title": "Dentist"}},
]
_SYNTHETIC_RESULT = {
    "type": "tool_result",
    "tool_use_id": "toolu_1",
    "content": _INTERRUPTED,
    "is_error": True,
}


class _HangingTool:
    """A registry whose one tool signals that it started, then never finishes."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.registry = ToolRegistry()

        async def create_event(title: str = "") -> str:
            self.started.set()
            await asyncio.Event().wait()
            return "created"

        self.registry.register(name="create_event", description="Create a calendar event", handler=create_event)


@pytest.fixture
def registry_plain() -> ToolRegistry:
    registry = ToolRegistry()

    async def create_event(title: str = "") -> str:
        return "created"

    registry.register(name="create_event", description="Create a calendar event", handler=create_event)
    return registry


@pytest.fixture
def hanging() -> _HangingTool:
    return _HangingTool()


def _last_user_blocks(request: dict[str, Any]) -> list[Any]:
    content = request["messages"][-1]["content"]
    assert request["messages"][-1]["role"] == "user"
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


async def test_chat_turn_after_an_interrupted_tool_appends_an_error_result_and_keeps_the_history(
    settings, session_manager, cost_tracker, hanging
):
    script = [([*_TOOL_CALL], "tool_use"), ([text("Done.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, hanging.registry)
        first = asyncio.create_task(agent.handle_message("user1", "test", "Book the dentist", memory=False))
        await asyncio.wait_for(hanging.started.wait(), timeout=5)
        first.cancel()  # the turn dies while its tool runs
        with contextlib.suppress(asyncio.CancelledError):
            await first
        session = await session_manager.get_or_create("user1", "test")
        assert session.messages[-1].tool_calls  # precondition: the history ends with an unanswered tool call

        await agent.handle_message("user1", "test", "Never mind", memory=False)

    first_request, second_request = llm.requests
    assert second_request["messages"][0] == first_request["messages"][0]  # the earlier message, unchanged
    assert second_request["messages"][1] == {"role": "assistant", "content": _TOOL_CALL_BLOCKS}
    assert _last_user_blocks(second_request)[0] == _SYNTHETIC_RESULT
    assert _last_user_blocks(second_request)[-1] == {"type": "text", "text": "Never mind"}


async def _hang_up_at_the_first_tool(stream) -> None:
    """Consume a streamed turn until its first tool starts, then close it (the client went away)."""
    async for chunk in stream:
        if chunk.type is StreamEventType.TOOL_START:
            break
    await stream.aclose()


def _voice_agent(settings, provider, session_manager, cost_tracker, registry) -> Agent:
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    return Agent(settings, provider, session_manager, cost_tracker, registry)


async def test_streaming_chat_turn_after_an_interrupted_tool_appends_an_error_result_and_keeps_the_history(
    settings, session_manager, cost_tracker, hanging
):
    script = [([*_TOOL_CALL], "tool_use"), ([text("Done.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, hanging.registry)
        await _hang_up_at_the_first_tool(agent.handle_message_stream("user1", "web", "Book the dentist", memory=False))
        session = await session_manager.get_or_create("user1", "web")
        assert session.messages[-1].tool_calls  # precondition: the history ends with an unanswered tool call

        _chunks = [c async for c in agent.handle_message_stream("user1", "web", "Never mind", memory=False)]

    first_request, second_request = llm.requests
    assert second_request["messages"][0] == first_request["messages"][0]
    assert second_request["messages"][1] == {"role": "assistant", "content": _TOOL_CALL_BLOCKS}
    assert _last_user_blocks(second_request)[0] == _SYNTHETIC_RESULT
    assert _last_user_blocks(second_request)[-1] == {"type": "text", "text": "Never mind"}


async def test_voice_turn_after_an_interrupted_tool_appends_an_error_result_and_keeps_the_history(
    settings, session_manager, cost_tracker, hanging
):
    script = [([*_TOOL_CALL], "tool_use"), ([text("Erledigt.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _voice_agent(settings, llm.provider, session_manager, cost_tracker, hanging.registry)
        await _hang_up_at_the_first_tool(agent.stream_voice_turn("caller1", "voice", "Termin beim Zahnarzt"))
        session = await session_manager.get_or_create("caller1", "voice")
        assert session.messages[-1].tool_calls  # precondition: the history ends with an unanswered tool call

        _chunks = [c async for c in agent.stream_voice_turn("caller1", "voice", "Egal")]

    first_request, second_request = llm.requests
    assert second_request["messages"][0] == first_request["messages"][0]
    assert second_request["messages"][1] == {"role": "assistant", "content": _TOOL_CALL_BLOCKS}
    assert _last_user_blocks(second_request)[0] == _SYNTHETIC_RESULT
    assert _last_user_blocks(second_request)[-1] == {"type": "text", "text": "Egal"}


async def test_only_the_unanswered_calls_of_a_partly_answered_tail_get_an_error_result(
    settings, session_manager, cost_tracker, registry_plain, caplog
):
    calls = [ToolCall(id=call_id, name="create_event", arguments={}) for call_id in ("toolu_a", "toolu_b", "toolu_c")]
    raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": {}} for c in calls]
    wire = "anthropic:claude-haiku-4-5-20251001"
    session = await session_manager.get_or_create("user1", "test")
    for message in (
        LLMMessage(role=MessageRole.USER, content="Book three"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls, raw=raw, wire=wire),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="toolu_b"),
    ):
        await session_manager.add_message(session, message)

    async with ScriptedAnthropic(settings, [([text("Done.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry_plain)
        with caplog.at_level(logging.WARNING, logger="pincer"):
            await agent.handle_message("user1", "test", "Never mind", memory=False)

    assert [r.getMessage() for r in caplog.records if "Stripping" in r.getMessage()] == []  # nothing needed repair
    results = [b for b in _last_user_blocks(llm.requests[0]) if b["type"] == "tool_result"]
    assert [(b["tool_use_id"], b.get("is_error")) for b in results] == [
        ("toolu_b", None),
        ("toolu_a", True),
        ("toolu_c", True),
    ]
    assert results[0]["content"] == "created"
    assert llm.requests[0]["messages"][1] == {"role": "assistant", "content": raw}


async def test_the_error_results_are_saved_with_their_flag_and_plain_results_stay_as_they_were(
    settings, session_manager, cost_tracker, registry_plain, tmp_path
):
    calls = [ToolCall(id=call_id, name="create_event", arguments={}) for call_id in ("toolu_a", "toolu_b")]
    session = await session_manager.get_or_create("user1", "test")
    for message in (
        LLMMessage(role=MessageRole.USER, content="Book two"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="toolu_b"),
    ):
        await session_manager.add_message(session, message)
    async with ScriptedAnthropic(settings, [([text("Done.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry_plain)
        await agent.handle_message("user1", "test", "Never mind", memory=False)

    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    saved = [m for m in json.loads(row["messages"]) if m["role"] == "tool_result"]
    assert saved == [
        {"role": "tool_result", "content": "created", "tool_call_id": "toolu_b"},
        {"role": "tool_result", "content": _INTERRUPTED, "tool_call_id": "toolu_a", "is_error": True},
    ]
    reloaded = SessionManager(tmp_path / "test.db")
    await reloaded.initialize()
    try:
        results = [m for m in (await reloaded.get_or_create("user1", "test")).messages if m.tool_call_id]
        assert [(m.tool_call_id, m.is_error) for m in results] == [("toolu_b", False), ("toolu_a", True)]
    finally:
        await reloaded.close()


async def test_the_error_results_of_five_open_calls_are_saved_in_one_write(
    settings, session_manager, cost_tracker, registry_plain
):
    """Every save writes the whole session: closing 5 calls is one write, not 5 back to back."""
    ids = [f"toolu_{n}" for n in range(5)]
    calls = [ToolCall(id=call_id, name="create_event", arguments={}) for call_id in ids]
    session = await session_manager.get_or_create("user1", "test")
    for message in (
        LLMMessage(role=MessageRole.USER, content="Book five"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls),
    ):
        await session_manager.add_message(session, message)
    saved_sizes: list[int] = []
    real_save = session_manager._store.save

    async def spy(**kwargs: Any) -> Any:
        saved_sizes.append(len(json.loads(kwargs["messages"])))
        return await real_save(**kwargs)

    session_manager._store.save = spy  # type: ignore[method-assign]
    async with ScriptedAnthropic(settings, [([text("Done.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry_plain)
        await agent.handle_message("user1", "test", "Never mind", memory=False)

    # The five results in one write (2 + 5 messages), then the user's message, then the reply
    assert saved_sizes == [7, 8, 9]
    results = [m for m in session.messages if m.role == MessageRole.TOOL_RESULT]
    assert [(m.tool_call_id, m.content, m.is_error) for m in results] == [(i, _INTERRUPTED, True) for i in ids]


async def test_a_turn_with_no_open_calls_saves_nothing_for_them(
    settings, session_manager, cost_tracker, registry_plain
):
    session = await session_manager.get_or_create("user1", "test")
    saved_sizes: list[int] = []
    real_save = session_manager._store.save

    async def spy(**kwargs: Any) -> Any:
        saved_sizes.append(len(json.loads(kwargs["messages"])))
        return await real_save(**kwargs)

    session_manager._store.save = spy  # type: ignore[method-assign]
    async with ScriptedAnthropic(settings, [([text("Done.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry_plain)
        await agent.handle_message("user1", "test", "Hello", memory=False)

    assert saved_sizes == [1, 2]  # the user's message, then the reply: no write for the calls there were none of
    assert len(session.messages) == 2


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_budget_refused_turn_leaves_an_unanswered_tail_as_it_was(
    settings, mock_llm, session_manager, registry_plain, tmp_path, streaming
):
    from pincer.config.pricing import TokenUsage, load_price_table
    from pincer.db.engine import get_database_url
    from pincer.llm.cost_tracker import CostTracker
    from pincer.services.costs import CostService

    call = ToolCall(id="toolu_1", name="create_event", arguments={})
    session = await session_manager.get_or_create("user1", "test")
    for message in (
        LLMMessage(role=MessageRole.USER, content="Book it"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
    ):
        await session_manager.add_message(session, message)
    before = [m.to_dict() for m in session.messages]

    # Today's spend is exactly the budget: test-fixture-model costs $7 per MTok, 1,000 tokens = $0.007
    await CostService(get_database_url(tmp_path / "test.db"), prices=load_price_table()).record(
        "anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000)
    )
    tracker = CostTracker(tmp_path / "test.db", daily_budget=0.007, prices=load_price_table())
    await tracker.initialize()
    try:
        agent = Agent(
            settings.model_copy(update={"daily_budget_usd": 0.007}), mock_llm, session_manager, tracker, registry_plain
        )
        if streaming:
            chunks = [c async for c in agent.handle_message_stream("user1", "test", "Again", memory=False)]
            refused = chunks[-1].budget_reached
        else:
            refused = (await agent.handle_message("user1", "test", "Again", memory=False)).budget_reached
    finally:
        await tracker.close()

    assert refused  # precondition: the turn was refused
    assert [m.to_dict() for m in session.messages] == before
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None and json.loads(row["messages"]) == before
