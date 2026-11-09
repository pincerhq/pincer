"""The daily budget is checked before each LLM call, and every completed call is recorded (issue #357).

`test-fixture-model` costs $7 per MTok for every token type (tests/fixtures/pricing), so a
call of 1,000 input tokens costs $0.007.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pincer.config.pricing import TokenUsage, load_price_table
from pincer.core.agent import Agent
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.base import BaseLLMProvider, LLMResponse, ToolCall
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
    queue = list(responses)
    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.side_effect = lambda *a, **k: queue.pop(0)
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
