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
    assert (reply.text, reply.budget_reached) == (_budget_text(_CALL_COST), True)
    assert reply.cost_usd == 0
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]
    session = await session_manager.get_or_create("user", "chat")
    assert [(m.role.value, m.content) for m in session.messages] == [("user", "hi")]  # the warning is not saved


async def test_a_refused_turn_stores_no_exchange_memory(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    from pincer.memory.store import MemoryStore

    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    memory = MemoryStore(tmp_path / "mem.db")
    await memory.initialize()
    agent = Agent(settings, _scripted_llm(_response()), session_manager, tracker, tool_registry, memory_store=memory)
    try:
        reply = await agent.handle_message("user", "chat", "hi")
        stored = await memory.list_memories(user_id="user", category="exchange")
    finally:
        await memory.close()

    assert reply.budget_reached  # precondition: refused
    assert stored == []


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
    assert session.messages[-1].tool_call_id == "tc2"  # the turn ends on its last tool result, no warning saved


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


async def _post_message(agent: Agent, text: str = "hi") -> httpx.Response:
    """One turn through the real `/api/chat/message` endpoint."""
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app

    app = create_app()
    app.state.agent = agent
    app.dependency_overrides[chat_user] = lambda: "user"
    body = {"text": text, "conversation_id": str(uuid.uuid4()), "memory": False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/chat/message", json=body)


async def test_a_chat_message_over_the_budget_is_a_402_with_the_budget_text(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """Clients key on the status (PR #366 review): a refusal is not a reply."""
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    response = await _post_message(agent)

    assert (response.status_code, response.json()) == (402, {"detail": _budget_text(_CALL_COST)})
    assert llm.complete.await_count == 0


async def test_a_chat_message_that_placed_a_call_before_the_budget_stop_answers_200() -> None:
    """The ringing call must reach the client, which then waits for its report."""
    from unittest.mock import MagicMock

    from pincer.core.agent import AgentResponse

    agent = MagicMock()
    agent.handle_message = AsyncMock(
        return_value=AgentResponse(text=_budget_text(0.01), call_sids=["CA9"], budget_reached=True)
    )

    response = await _post_message(agent)

    assert response.status_code == 200
    assert (response.json()["reply"], response.json()["call_sids"]) == (_budget_text(0.01), ["CA9"])


async def test_a_web_chat_turn_with_spend_at_the_budget_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    events = await _web_chat(agent)

    assert events == [("error", {"message": _budget_text(_CALL_COST), "cost_usd": 0.0})]
    assert (llm.complete.await_count, len(llm.streams)) == (0, 0)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]
    session = await session_manager.get_or_create("user", "web")
    assert [m.role.value for m in session.messages if m.role.value == "assistant"] == []


async def _streamed_turn(agent: Agent, text: str = "hi") -> list[Any]:
    return [chunk async for chunk in agent.handle_message_stream("user", "web", text, memory=False)]


async def test_a_plain_streamed_turn_records_its_one_request(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """Every step of a web chat turn is one streamed request (slice 13)."""
    settings, tracker = await budgeted(100.0)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (0, 1)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]
    assert chunks[-1].content == "Done."
    assert chunks[-1].cost_usd == pytest.approx(_CALL_COST)


async def test_a_streamed_turn_with_one_tool_round_records_two_requests(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """The tool call, then the streamed reply."""
    settings, tracker = await budgeted(100.0)
    llm = _scripted_llm(_response(_greet(1)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (0, 2)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 2
    assert chunks[-1].cost_usd == pytest.approx(0.014)


async def test_a_streamed_tool_loop_that_crosses_the_budget_stops_before_its_next_request(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(_greet(1)), _response(_greet(2)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent, "greet Ann twice")

    assert (llm.complete.await_count, len(llm.streams)) == (0, 2)
    assert [c.content for c in chunks if c.type.value == "text"] == []  # the warning is no streamed text
    assert (chunks[-1].content, chunks[-1].cost_usd) == (_budget_text(0.01), pytest.approx(0.014))
    assert chunks[-1].budget_reached
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 2
    session = await session_manager.get_or_create("user", "web")
    calls = [tc.id for m in session.messages for tc in m.tool_calls]
    results = [m.tool_call_id for m in session.messages if m.tool_call_id]
    assert calls == results == ["tc1", "tc2"]
    assert session.messages[-1].tool_call_id == "tc2"  # no warning saved


async def test_the_reply_step_after_a_tool_round_is_refused_at_the_budget(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """$0.007 seeded + the $0.007 tool call = $0.014, the budget: the reply step is a request too."""
    settings, tracker = await budgeted(0.014)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response(_greet(1)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = await _streamed_turn(agent)

    assert (llm.complete.await_count, len(llm.streams)) == (0, 1)
    assert (chunks[-1].content, chunks[-1].cost_usd) == (_budget_text(0.014), pytest.approx(_CALL_COST))
    assert chunks[-1].budget_reached
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 2
    session = await session_manager.get_or_create("user", "web")
    assert [m.tool_call_id for m in session.messages if m.tool_call_id] == ["tc1"]
    assert session.messages[-1].tool_call_id == "tc1"  # no warning saved


async def test_the_web_chat_done_event_carries_the_cost_of_the_turn_s_recorded_requests(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(0.0)
    llm = _scripted_llm(_response(_greet(1)), _response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    events = await _web_chat(agent)

    assert [name for name, _ in events] == ["tool", "tool", "chunk", "done"]
    stored = await _stored_costs(tmp_path)
    assert len(stored) == 2
    assert events[-1] == ("done", {"text": "Done.", "cost_usd": pytest.approx(sum(stored), abs=1e-12)})
    assert events[-1][1]["cost_usd"] == pytest.approx(0.014)


# ── Phone calls (slice 6): a call in progress runs; a new one may not start ──

_REJECT_BUSY = '<Response><Reject reason="busy"/></Response>'


@pytest.fixture
async def phone_settings(settings: Settings) -> Callable[..., Awaitable[Settings]]:
    """A factory: `await phone_settings(budget, spent=n)` gives settings whose own database
    (settings.db_path, the one the webhook and the dial read) holds n calls' spend."""
    from pincer.db import ensure_schema_current

    async def make(budget: float, *, spent: int = 0) -> Settings:
        ensure_schema_current(settings.db_path)
        service = CostService(get_database_url(settings.db_path), prices=load_price_table())
        for _ in range(spent):
            await service.record("anthropic", _MODEL, _CALL)
        return settings.model_copy(update={"daily_budget_usd": budget, "voice_allowed_callers": "*"})

    return make


@pytest.fixture
def voice_routes(monkeypatch: pytest.MonkeyPatch) -> Callable[[Settings], tuple[httpx.AsyncClient, AsyncMock]]:
    """The real Twilio router on a mock engine; the module globals are restored afterwards."""
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from pincer.voice import twiml_server

    for name in ("_engine", "_settings", "_memory"):
        monkeypatch.setattr(twiml_server, name, getattr(twiml_server, name))

    def make(settings: Settings) -> tuple[httpx.AsyncClient, AsyncMock]:
        engine = AsyncMock()
        engine.get_active_calls = MagicMock(return_value={})
        engine.get_call_state = MagicMock(return_value=None)
        twiml_server.init_voice_routes(engine, settings)
        app = FastAPI()
        app.include_router(twiml_server.twilio_router)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), engine

    return make


async def _inbound(client: httpx.AsyncClient, call_sid: str = "CA_b") -> httpx.Response:
    async with client:
        return await client.post(
            "/api/apps/twilio/webhook", data={"CallSid": call_sid, "From": "+14155551234", "To": "+14155559876"}
        )


async def test_an_inbound_call_over_the_budget_is_rejected_busy_before_it_is_answered(  # type: ignore[no-untyped-def]
    phone_settings, voice_routes
) -> None:
    client, engine = voice_routes(await phone_settings(0.007, spent=1))

    response = await _inbound(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert response.content == _REJECT_BUSY.encode()
    engine.on_call_start.assert_not_awaited()


@pytest.fixture
async def telemetry(settings: Settings) -> AsyncIterator[Path]:
    """The live telephony telemetry runtime, on the settings' own database."""
    from pincer.db import ensure_schema_current
    from pincer.voice.telemetry import runtime
    from pincer.voice.telemetry.recorder import get_recorder

    ensure_schema_current(settings.db_path)
    runtime.reset_for_tests(db_path=str(settings.db_path))
    await get_recorder().start()
    yield settings.db_path
    await runtime.shutdown()
    runtime.reset_for_tests()


async def _settle() -> None:
    """Make every telemetry write so far durable."""
    from pincer.voice.telemetry.recorder import get_recorder
    from pincer.voice.telemetry.tracer import drain_pending

    await drain_pending()
    await get_recorder().flush()


async def _telephony_call(db_path: Path, provider_call_id: str) -> dict[str, Any] | None:
    from pincer.voice.telemetry import queries

    await _settle()
    return await queries.get_call(db_path, provider_call_id)


def _refusal(call: dict[str, Any] | None) -> tuple[Any, ...]:
    assert call is not None, "the refusal left no call record"
    return (call["status"], call["outcome"], call["failure_code"], call["termination_reason"])


async def test_an_inbound_call_rejected_over_the_budget_is_logged_and_recorded_with_reason_budget(  # type: ignore[no-untyped-def]
    phone_settings, voice_routes, telemetry, caplog
) -> None:
    from pincer.services.voice import CallsService

    settings = await phone_settings(0.007, spent=1)
    client, _ = voice_routes(settings)

    with caplog.at_level("WARNING", logger="pincer.voice.twiml_server"):
        await _inbound(client, "CA_b")

    assert "Inbound call CA_b declined: daily budget reached" in caplog.messages
    row = await (await CallsService.for_path(settings.db_path)).get("CA_b")
    assert row is not None
    assert (row["direction"], row["failure_code"]) == ("inbound", "budget")
    assert _refusal(await _telephony_call(telemetry, "CA_b")) == ("failed", "declined", "budget", "budget")


@pytest.mark.parametrize(("budget", "spent"), [(0.014, 1), (0.0, 3)], ids=["below-the-budget", "no-budget"])
async def test_an_inbound_call_under_the_budget_is_answered(  # type: ignore[no-untyped-def]
    phone_settings, voice_routes, budget, spent
) -> None:
    client, engine = voice_routes(await phone_settings(budget, spent=spent))

    response = await _inbound(client)

    assert b"<Connect>" in response.content
    engine.on_call_start.assert_awaited_once()


_DIAL_REFUSAL = "Error: daily budget reached, call not placed."


@pytest.fixture
def dialling(monkeypatch: pytest.MonkeyPatch, fake_twilio_rest: Any) -> Callable[[Settings], Settings]:
    """`dialling(settings)`: outbound calling on, the process settings, and no engine in this process,
    so a placed call reaches `fake_twilio_rest.creates`. The per-user daily counts start empty."""
    from pydantic import SecretStr

    from pincer.voice import outbound

    monkeypatch.setattr("pincer.voice.twiml_server._engine", None)
    monkeypatch.setattr(outbound, "_daily_outbound_counts", {})

    def make(settings: Settings) -> Settings:
        voice = settings.model_copy(
            update={
                "voice_enabled": True,
                "voice_outbound_enabled": True,
                "voice_webhook_base_url": "https://example.com",
                "twilio_account_sid": "AC123",
                "twilio_auth_token": SecretStr("token"),
                "twilio_phone_number": "+15550001111",
            }
        )
        monkeypatch.setattr("pincer.config.get_settings", lambda: voice)
        return voice

    return make


async def _dial(user_id: str = "u1") -> str:
    from pincer.voice.outbound import make_phone_call

    return await make_phone_call(
        "+14155551234", "Confirm the dentist appointment", context={"user_id": user_id, "channel": "telegram"}
    )


async def _outbound_rows(db_path: Path) -> list[dict[str, Any]]:
    from pincer.voice.telemetry import queries

    await _settle()
    page = await queries.search_calls(db_path, queries.CallFilters.for_hours(1))
    return [c for c in page["calls"] if c["direction"] == "outbound"]


async def test_the_dial_tool_over_the_budget_places_no_call_and_returns_the_refusal(  # type: ignore[no-untyped-def]
    phone_settings, dialling, fake_twilio_rest, telemetry, caplog
) -> None:
    from pincer.voice import outbound

    dialling(await phone_settings(0.007, spent=1))

    with caplog.at_level("WARNING", logger="pincer.voice.outbound"):
        result = await _dial("u1")

    assert result == _DIAL_REFUSAL
    assert fake_twilio_rest.creates == []
    assert outbound._daily_outbound_counts == {}, "a refused call must not count toward the daily limit"
    assert "make_phone_call refused for user u1: daily budget reached" in caplog.messages
    rows = await _outbound_rows(telemetry)
    assert len(rows) == 1
    assert rows[0]["provider_call_id"].startswith("pending-")
    assert _refusal(rows[0]) == ("failed", "declined", "budget", "budget")


@pytest.mark.parametrize(("budget", "spent"), [(0.014, 1), (0.0, 3)], ids=["below-the-budget", "no-budget"])
async def test_the_dial_tool_under_the_budget_places_the_call(  # type: ignore[no-untyped-def]
    phone_settings, dialling, fake_twilio_rest, budget, spent
) -> None:
    dialling(await phone_settings(budget, spent=spent))

    result = await _dial()

    assert result.startswith("Call initiated successfully.")
    assert len(fake_twilio_rest.creates) == 1


async def test_a_dashboard_call_over_the_budget_is_a_402_with_the_refusal(  # type: ignore[no-untyped-def]
    phone_settings, dialling, fake_twilio_rest
) -> None:
    """A spend limit, not a rate limit (429) and not a Twilio failure (502)."""
    from fastapi import FastAPI

    from pincer.api.voice import router

    dialling(await phone_settings(0.007, spent=1))
    app = FastAPI()
    app.include_router(router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/voice/calls",
            json={"target_number": "+14155551234", "purpose": "Confirm the dentist appointment"},
        )

    assert (response.status_code, response.json()) == (402, {"detail": "daily budget reached, call not placed."})
    assert fake_twilio_rest.creates == []


async def test_a_dial_asked_for_by_the_request_that_crossed_the_budget_is_refused_and_the_turn_ends(  # type: ignore[no-untyped-def]
    phone_settings, dialling, fake_twilio_rest, session_manager
) -> None:
    """The dialling request ($0.007) brings spend to the budget: the real dial tool refuses,
    the refusal is the tool's result, and the loop stops before its next request."""
    from pincer.llm.base import MessageRole
    from pincer.tools.registry import ToolRegistry
    from pincer.voice.outbound import make_phone_call

    settings = dialling(await phone_settings(0.007))
    tracker = CostTracker(settings.db_path, daily_budget=0.007, prices=load_price_table())
    await tracker.initialize()
    registry = ToolRegistry()
    registry.register(
        name="make_phone_call",
        description="Places a call",
        handler=make_phone_call,
        parameters={
            "type": "object",
            "properties": {"target_number": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["target_number", "purpose"],
        },
    )
    dial = ToolCall(
        id="tc1",
        name="make_phone_call",
        arguments={"target_number": "+14155551234", "purpose": "Confirm the dentist appointment"},
    )
    llm = _scripted_llm(_response([dial]), _response())
    agent = Agent(settings, llm, session_manager, tracker, registry)

    try:
        result = await agent.handle_message("user1", "test", "call the dentist", memory=False)
    finally:
        await tracker.close()

    assert fake_twilio_rest.creates == []
    assert llm.complete.await_count == 1
    assert (result.text, result.call_sids) == (_budget_text(0.007), [])
    session = await session_manager.get_or_create("user1", "test")
    tool_results = [m.content for m in session.messages if m.role == MessageRole.TOOL_RESULT]
    assert tool_results == [_DIAL_REFUSAL]


async def test_an_utterance_in_a_call_in_progress_runs_and_is_recorded_over_the_budget(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """Q11: a call in progress is never gated, warned or cut off, and budget figures are never spoken."""
    from pincer.core.agent import StreamEventType

    settings, tracker = await budgeted(0.007)
    await _seed(tmp_path, _CALL, _CALL)  # $0.014: over the limit before the utterance
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    chunks = [c async for c in agent.stream_voice_turn("user", "voice", "hi", session_key="CA_live")]

    assert len(llm.streams) == 1
    spoken = [c.content for c in chunks if c.type == StreamEventType.TEXT]
    assert (spoken, chunks[-1].type, chunks[-1].content) == (["Done."], StreamEventType.DONE, "Done.")
    assert not any("budget" in c.content.lower() or "$" in c.content for c in chunks)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)] * 3


def test_nothing_is_refused_by_an_exception_after_the_call_any_more() -> None:
    """The budget is checked before a call and every completed call is recorded, so the old
    post-call refusal (and its catchers, which spoke the budget figures on a call) is gone."""
    import pincer.exceptions

    assert not hasattr(pincer.exceptions, "BudgetExceededError")
