"""The daily budget is checked before each LLM call, and every completed call is recorded (issue #357).

`test-fixture-model` costs $7 per MTok for every token type (tests/fixtures/pricing), so a
call of 1,000 input tokens costs $0.007.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from pincer.config.pricing import TokenUsage, load_price_table
from pincer.core.agent import Agent
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.base import BaseLLMProvider, LLMResponse, StreamTurnEvent, ToolCall
from pincer.llm.cost_tracker import CostTracker
from pincer.repositories.costs import CostLogRepository
from pincer.services.costs import CostService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from pincer.config import Settings

_MODEL = "test-fixture-model"
_CALL = TokenUsage(input_tokens=1_000)  # $0.007
_CALL_COST = 0.007


def _response(tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        content="" if tool_calls else "Done.",
        tool_calls=tool_calls or [],
        request_model=_MODEL,
        stop_reason="tool_use" if tool_calls else "end_turn",
        input_tokens=_CALL.input_tokens,
    )


def _greet(n: int) -> list[ToolCall]:
    return [ToolCall(id=f"tc{n}", name="greet", arguments={"name": "Ann"})]


def _scripted_llm(*responses: LLMResponse) -> AsyncMock:
    """`complete` and `stream_turn` take the next scripted response; `stream_turn.calls` counts streams."""
    queue = list(responses)
    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.side_effect = lambda *a, **k: queue.pop(0)
    streams: list[dict[str, Any]] = []

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        streams.append(kwargs)
        response = queue.pop(0)
        if response.content:
            yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    llm.stream_turn = stream_turn
    llm.streams = streams
    return llm


@pytest.fixture
async def budgeted(
    tmp_path: Path, settings: Settings
) -> AsyncIterator[Callable[[float], Awaitable[tuple[Settings, CostTracker]]]]:
    """A factory: `await budgeted(budget)` gives settings and a tracker on the session DB with that daily budget."""
    trackers: list[CostTracker] = []

    async def make(budget: float) -> tuple[Settings, CostTracker]:
        tracker = CostTracker(tmp_path / "test.db", daily_budget=budget, prices=load_price_table())
        await tracker.initialize()
        trackers.append(tracker)
        return settings.model_copy(update={"daily_budget_usd": budget}), tracker

    yield make
    for tracker in trackers:
        await tracker.close()


async def _seed(tmp_path: Path, *usages: TokenUsage) -> None:
    """Spend recorded earlier today, by a service with no budget of its own."""
    service = CostService(get_database_url(tmp_path / "test.db"), prices=load_price_table())
    for usage in usages:
        await service.record("anthropic", _MODEL, usage)


async def _stored_costs(tmp_path: Path) -> list[float]:
    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        rows = await CostLogRepository(session).list()
    return [r.cost_usd for r in sorted(rows, key=lambda r: r.timestamp)]


def _budget_text(limit: float) -> str:
    return (
        f"Warning: Daily budget limit reached. Limit: ${limit:.2f}. "
        "I'll be back tomorrow, or you can increase the limit."
    )


async def test_a_turn_with_spend_at_the_budget_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)  # today's spend is exactly the budget
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    reply = await agent.handle_message("user", "chat", "hi", memory=False)

    assert llm.complete.await_count == 0
    assert reply.text == _budget_text(_CALL_COST)
    assert reply.cost_usd == 0
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]


async def test_the_call_that_crosses_the_budget_is_recorded_and_the_next_turn_is_refused(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(), _response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    first = await agent.handle_message("user", "chat", "one", memory=False)
    crossing = await agent.handle_message("user", "chat", "two", memory=False)  # $0.007 spent, under $0.01
    refused = await agent.handle_message("user", "chat", "three", memory=False)  # $0.014 spent

    assert (first.text, crossing.text) == ("Done.", "Done.")
    assert crossing.cost_usd == pytest.approx(_CALL_COST)
    assert refused.text == _budget_text(0.01)
    assert llm.complete.await_count == 2
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST), pytest.approx(_CALL_COST)]
    assert await tracker.get_today_spend() == pytest.approx(0.014)


async def test_a_tool_loop_that_crosses_the_budget_stops_before_its_next_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(_greet(1)), _response(_greet(2)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    reply = await agent.handle_message("user", "chat", "greet Ann twice", memory=False)

    assert llm.complete.await_count == 2  # the third request, after $0.014, is never sent
    assert reply.text == _budget_text(0.01)
    assert reply.cost_usd == pytest.approx(0.014)
    assert reply.tool_calls_made == 2
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST), pytest.approx(_CALL_COST)]
    session = await session_manager.get_or_create("user", "chat")
    calls = [tc.id for m in session.messages for tc in m.tool_calls]
    results = [m.tool_call_id for m in session.messages if m.tool_call_id]
    assert calls == results == ["tc1", "tc2"]
    assert session.messages[-1].content == _budget_text(0.01)


async def test_a_budget_of_zero_never_refuses(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.0)
    await _seed(tmp_path, TokenUsage(input_tokens=150_000_000))  # $1,050 today
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    reply = await agent.handle_message("user", "chat", "hi", memory=False)

    assert llm.complete.await_count == 1
    assert reply.text == "Done."
    assert await _stored_costs(tmp_path) == [pytest.approx(1050.0), pytest.approx(_CALL_COST)]


def _headless_budget_text(limit: float) -> str:
    return f"Warning: Daily budget limit reached. Limit: ${limit:.2f}. Could not complete this scheduled action."


async def test_a_headless_run_with_spend_at_the_budget_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="user", channel="scheduled")

    assert llm.complete.await_count == 0
    assert result == _headless_budget_text(_CALL_COST)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]


async def test_a_headless_tool_loop_that_crosses_the_budget_stops_before_its_next_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(_greet(1)), _response(_greet(2)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    result = await agent.run_headless("greet Ann twice", user_id="user", channel="scheduled")

    assert llm.complete.await_count == 2
    assert result == _headless_budget_text(0.01)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST), pytest.approx(_CALL_COST)]


# ── web chat (the streaming path) ───────────────────────────────────


async def _web_chat(agent: Agent, text: str = "hi") -> list[tuple[str, dict[str, Any]]]:
    """One turn through the real `/api/chat/stream` endpoint: its SSE events, in order."""
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app

    app = create_app()
    app.state.agent = agent
    app.dependency_overrides[chat_user] = lambda: "user"
    body = {"text": text, "conversation_id": str(uuid.uuid4()), "memory": False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/stream", json=body)
    assert response.status_code == 200
    events = []
    for frame in response.text.split("\n\n"):
        if frame.strip():
            event, data = frame.split("\n", 1)
            events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


async def test_a_web_chat_turn_with_spend_at_the_budget_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    events = await _web_chat(agent)

    assert events == [
        ("chunk", {"delta": _budget_text(_CALL_COST)}),
        ("done", {"text": _budget_text(_CALL_COST), "cost_usd": 0.0}),
    ]
    assert (llm.complete.await_count, len(llm.streams)) == (0, 0)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]


async def _streamed_turn(agent: Agent, text: str = "hi") -> list[Any]:
    return [chunk async for chunk in agent.handle_message_stream("user", "web", text, memory=False)]


async def test_a_plain_streamed_turn_records_both_its_requests(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """The tool loop's toolless answer is discarded and the reply streamed by a second request: both are spent."""
    settings, tracker = await budgeted(100.0)
    llm = _scripted_llm(_response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (1, 1)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST), pytest.approx(_CALL_COST)]
    assert chunks[-1].content == "Done."
    assert chunks[-1].cost_usd == pytest.approx(0.014)


async def test_a_streamed_turn_with_one_tool_round_records_three_requests(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """Tool call, the toolless answer the stream replaces, then the streamed reply."""
    settings, tracker = await budgeted(100.0)
    llm = _scripted_llm(_response(_greet(1)), _response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (2, 1)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 3
    assert chunks[-1].cost_usd == pytest.approx(0.021)


async def test_a_streamed_tool_loop_that_crosses_the_budget_stops_before_its_next_request(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(_greet(1)), _response(_greet(2)), _response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent, "greet Ann twice")

    assert (llm.complete.await_count, len(llm.streams)) == (2, 0)
    assert [c.content for c in chunks if c.type.value == "text"] == [_budget_text(0.01)]
    assert (chunks[-1].content, chunks[-1].cost_usd) == (_budget_text(0.01), pytest.approx(0.014))
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 2
    session = await session_manager.get_or_create("user", "web")
    calls = [tc.id for m in session.messages for tc in m.tool_calls]
    results = [m.tool_call_id for m in session.messages if m.tool_call_id]
    assert calls == results == ["tc1", "tc2"]
    assert session.messages[-1].content == _budget_text(0.01)


async def test_the_final_stream_is_not_requested_once_the_tool_loop_reached_the_budget(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """$0.007 seeded + the toolless $0.007 answer = $0.014, the budget: the streamed reply is a request too."""
    settings, tracker = await budgeted(0.014)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (1, 0)
    assert (chunks[-1].content, chunks[-1].cost_usd) == (_budget_text(0.014), pytest.approx(_CALL_COST))
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 2


async def test_the_web_chat_done_event_carries_the_cost_of_the_turn_s_recorded_requests(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.0)
    llm = _scripted_llm(_response(_greet(1)), _response(), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    events = await _web_chat(agent)

    assert [name for name, _ in events] == ["tool", "tool", "chunk", "done"]
    stored = await _stored_costs(tmp_path)
    assert len(stored) == 3
    assert events[-1] == ("done", {"text": "Done.", "cost_usd": pytest.approx(sum(stored), abs=1e-12)})
    assert events[-1][1]["cost_usd"] == pytest.approx(0.021)
