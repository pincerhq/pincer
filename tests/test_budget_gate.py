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
from pincer.core.session import conversation_key
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


_STOPPED_MARKER = "(Stopped before answering: the daily budget was reached.)"


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
    assert session.messages == []  # a refused turn persists nothing, its user message included


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
    # Closed by the marker, not the warning: no dangling tool result (PR #366 review)
    assert (session.messages[-1].role.value, session.messages[-1].content) == ("assistant", _STOPPED_MARKER)


async def test_the_turn_after_a_budget_stop_sends_a_well_formed_history(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    """Once the budget allows again, the model sees the stopped turn closed by the marker (PR #366 review)."""
    settings, tracker = await budgeted(0.01)
    llm = _scripted_llm(_response(_greet(1)), _response(_greet(2)), _response())
    stopped = await Agent(settings, llm, session_manager, tracker, tool_registry).handle_message(
        "user", "chat", "greet Ann twice", memory=False
    )
    open_settings, open_tracker = await budgeted(0.0)
    requests: list[list[Any]] = []
    answer = llm.complete.side_effect
    llm.complete.side_effect = lambda *a, **k: requests.append(list(k["messages"])) or answer(*a, **k)

    await Agent(open_settings, llm, session_manager, open_tracker, tool_registry).handle_message(
        "user", "chat", "and now?", memory=False
    )

    assert stopped.budget_reached  # precondition: the first turn was stopped mid-way
    [sent] = requests  # the messages as sent, before the reply is appended
    assert [(m.role.value, [tc.id for tc in m.tool_calls], m.content) for m in sent] == [
        ("user", [], "greet Ann twice"),
        ("assistant", ["tc1"], ""),
        ("tool_result", [], sent[2].content),
        ("assistant", ["tc2"], ""),
        ("tool_result", [], sent[4].content),
        ("assistant", [], _STOPPED_MARKER),
        ("user", [], "and now?"),
    ]


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


async def _web_chat(
    agent: Agent, text: str = "hi", conversation_id: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """One turn through the real `/api/chat/stream` endpoint: its SSE events, in order."""
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app

    app = create_app()
    app.state.agent = agent
    app.dependency_overrides[chat_user] = lambda: "user"
    body = {"text": text, "conversation_id": conversation_id or str(uuid.uuid4()), "memory": False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/stream", json=body)
    assert response.status_code == 200
    events = []
    for frame in response.text.split("\n\n"):
        if frame.strip():
            event, data = frame.split("\n", 1)
            events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


async def _post_message(agent: Agent, text: str = "hi", conversation_id: str | None = None) -> httpx.Response:
    """One turn through the real `/api/chat/message` endpoint."""
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app

    app = create_app()
    app.state.agent = agent
    app.dependency_overrides[chat_user] = lambda: "user"
    body = {"text": text, "conversation_id": conversation_id or str(uuid.uuid4()), "memory": False}
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
    body = response.json()
    # The reply is the model's own text (none here); the warning has its own field (PR #366 review)
    assert (body["reply"], body["budget_warning"], body["call_sids"]) == ("", _budget_text(0.01), ["CA9"])


async def test_a_web_chat_turn_with_spend_at_the_budget_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(_CALL_COST)
    await _seed(tmp_path, _CALL)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    conversation = str(uuid.uuid4())

    events = await _web_chat(agent, conversation_id=conversation)

    assert events == [("error", {"message": _budget_text(_CALL_COST), "cost_usd": 0.0})]
    assert (llm.complete.await_count, len(llm.streams)) == (0, 0)
    assert await _stored_costs(tmp_path) == [pytest.approx(_CALL_COST)]
    session = await session_manager.get_or_create(
        "user", "web", session_key=conversation_key("user", "web", conversation)
    )
    assert session.messages == []  # a refused turn persists nothing, its user message included


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
    assert (session.messages[-1].role.value, session.messages[-1].content) == ("assistant", _STOPPED_MARKER)


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
    assert (session.messages[-1].role.value, session.messages[-1].content) == ("assistant", _STOPPED_MARKER)


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
        # A server error comes back as the 500 Twilio would get, not as a raised exception
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="http://test"), engine

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
    so a placed call reaches `fake_twilio_rest.creates`. The per-user daily counts start empty, and
    there are no quiet hours: the shipped 20:00-08:00 window would refuse the call on the wall clock."""
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
                "voice_quiet_hours": "",  # as in test_voice_outbound_gate: these tests are not about time
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


# ── The budget cannot be read (PR #366 review): inbound fails open, outbound fails closed ──


def _unreadable_cost_db(settings: Settings, budget: float = 0.007) -> Settings:
    """Settings whose database is a file of non-database bytes: every read raises `DatabaseError`."""
    unreadable = settings.data_dir / "unreadable"
    unreadable.mkdir(parents=True, exist_ok=True)
    (unreadable / "pincer.db").write_bytes(b"not a database, " * 64)
    return settings.model_copy(
        update={"data_dir": unreadable, "daily_budget_usd": budget, "voice_allowed_callers": "*"}
    )


async def test_an_inbound_call_is_answered_when_the_budget_cannot_be_read(  # type: ignore[no-untyped-def]
    settings, voice_routes, caplog
) -> None:
    """A caller is on the line: Twilio's "application error" is worse than one call over the budget."""
    client, engine = voice_routes(_unreadable_cost_db(settings))

    with caplog.at_level("WARNING", logger="pincer.voice.twiml_server"):
        response = await _inbound(client, "CA_db")

    assert response.status_code == 200
    assert b"<Connect>" in response.content
    engine.on_call_start.assert_awaited_once()
    assert "Inbound call CA_db: daily budget check failed — answering the call" in caplog.messages


async def test_the_dial_tool_places_no_call_when_the_budget_cannot_be_read(  # type: ignore[no-untyped-def]
    settings, dialling, fake_twilio_rest, caplog
) -> None:
    """Nobody is waiting on a ringing line, and every surface can retry: refuse, in the tool's own words."""
    from pincer.voice import outbound

    dialling(_unreadable_cost_db(settings))

    with caplog.at_level("WARNING", logger="pincer.voice.outbound"):
        result = await _dial("u1")

    assert result == "Error: could not check the daily budget, call not placed."
    assert fake_twilio_rest.creates == []
    assert outbound._daily_outbound_counts == {}
    assert "make_phone_call refused for user u1: daily budget check failed" in caplog.messages


async def test_a_dashboard_call_is_a_503_when_the_budget_cannot_be_read(  # type: ignore[no-untyped-def]
    settings, dialling, fake_twilio_rest
) -> None:
    """A dependency that is down, not a Twilio failure (502)."""
    from fastapi import FastAPI

    from pincer.api.voice import router

    dialling(_unreadable_cost_db(settings))
    app = FastAPI()
    app.include_router(router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/voice/calls",
            json={"target_number": "+14155551234", "purpose": "Confirm the dentist appointment"},
        )

    assert (response.status_code, response.json()) == (
        503,
        {"detail": "could not check the daily budget, call not placed."},
    )
    assert fake_twilio_rest.creates == []


# ── A cost that cannot be stored (PR #366 review): the call was paid, the turn goes on ──


def _not_a_database(tmp_path: Path) -> str:
    """The URL of a file of non-database bytes: every write raises `DatabaseError`."""
    path = tmp_path / "broken" / "pincer.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database, " * 64)
    return get_database_url(path)


async def test_a_cost_that_cannot_be_stored_is_logged_and_still_returned(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    service = CostService(_not_a_database(tmp_path), prices=load_price_table())

    with caplog.at_level("ERROR", logger="pincer.services.costs"):
        cost = await service.record("anthropic", _MODEL, _CALL)

    assert cost == pytest.approx(_CALL_COST)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert _MODEL in errors[0].getMessage()
    assert errors[0].exc_info is not None


async def _drop_cost_table(tmp_path: Path) -> None:
    """Every later cost write (and read) on the session DB raises: the table is gone."""
    from sqlalchemy import text

    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        await (await session.connection()).execute(text("DROP TABLE pincer_cost_logs"))


async def test_a_metered_request_whose_cost_cannot_be_stored_still_returns_its_response(  # type: ignore[no-untyped-def]
    tmp_path, budgeted
) -> None:
    """A finished call's outcome or a thread summary: the paid answer is used, not dropped as "no answer"."""
    from pincer.llm.cost_tracker import MeteredLLM

    _, tracker = await budgeted(0.0)  # no budget: nothing is read
    await _drop_cost_table(tmp_path)
    llm = _scripted_llm(_response())

    response = await MeteredLLM(llm, tracker, "anthropic").complete([])

    assert response.content == "Done."


# ── A budget stop after the turn placed a call: the client keeps waiting for its report ──


def _dial_registry(then: Callable[[], Awaitable[None]] | None = None) -> Any:
    """A registry whose make_phone_call places call CA9, then runs `then`."""
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def make_phone_call(context: dict[str, Any] | None = None) -> str:
        if then is not None:
            await then()
        return "Call initiated successfully.\nCall SID: CA9\nTo: +4930123\nPurpose: x\n"

    registry.register(
        name="make_phone_call",
        description="Places a call",
        handler=make_phone_call,
        parameters={"type": "object", "properties": {}},
    )
    return registry


def _dial_call() -> list[ToolCall]:
    return [ToolCall(id="tc1", name="make_phone_call", arguments={})]


async def test_a_web_chat_turn_that_placed_a_call_before_the_budget_stop_ends_with_done(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager
) -> None:
    """As `POST /message` answers 200: an `error` would end the turn and the wait for the call's report."""
    settings, tracker = await budgeted(_CALL_COST)  # the dialling request crosses it
    llm = _scripted_llm(_response(_dial_call()), _response())
    agent = Agent(settings, llm, session_manager, tracker, _dial_registry())

    events = await _web_chat(agent, "call them")

    assert events == [
        ("tool", {"phase": "start", "name": "Using make_phone_call..."}),
        ("tool", {"phase": "done", "name": "make_phone_call", "call_sid": "CA9"}),
        ("done", {"text": "", "cost_usd": pytest.approx(_CALL_COST), "budget_warning": _budget_text(_CALL_COST)}),
    ]
    assert len(llm.streams) == 1


# ── The budget cannot be read during a turn (PR #366 review, lead ruling 2026-11-10) ──

_CHECK_FAILED_TEXT = "Warning: Could not check the daily budget, so I stopped before answering. Please try again."
_CHECK_FAILED_MARKER = "(Stopped before answering: the daily budget could not be checked.)"


async def _conversation_messages(session_manager: Any, conversation: str) -> list[Any]:
    session = await session_manager.get_or_create(
        "user", "web", session_key=conversation_key("user", "web", conversation)
    )
    return list(session.messages)


async def test_a_chat_message_whose_budget_cannot_be_read_is_a_503_and_saves_nothing(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(1.0)
    await _drop_cost_table(tmp_path)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)
    conversation = str(uuid.uuid4())

    response = await _post_message(agent, conversation_id=conversation)

    # One status whenever the budget cannot be read, before the turn or mid-way (PR #366 review)
    assert (response.status_code, response.json()) == (503, {"detail": _CHECK_FAILED_TEXT})
    assert llm.complete.await_count == 0
    assert await _conversation_messages(session_manager, conversation) == []


async def test_a_web_chat_turn_whose_budget_cannot_be_read_is_one_error_and_saves_nothing(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry
) -> None:
    settings, tracker = await budgeted(1.0)
    await _drop_cost_table(tmp_path)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)
    conversation = str(uuid.uuid4())

    events = await _web_chat(agent, conversation_id=conversation)

    assert events == [("error", {"message": _CHECK_FAILED_TEXT, "cost_usd": 0.0})]
    assert (llm.complete.await_count, len(llm.streams)) == (0, 0)
    assert await _conversation_messages(session_manager, conversation) == []


async def test_a_turn_that_loses_the_budget_mid_way_stops_closed_and_reports_its_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, caplog
) -> None:
    """Fails closed: no new paid request, the history closed by a marker, the ringing call still reported."""
    settings, tracker = await budgeted(1.0)
    llm = _scripted_llm(_response(_dial_call()), _response())
    agent = Agent(settings, llm, session_manager, tracker, _dial_registry(then=lambda: _drop_cost_table(tmp_path)))

    with caplog.at_level("ERROR", logger="pincer.core.agent"):
        reply = await agent.handle_message("user", "chat", "call them", memory=False)

    assert llm.complete.await_count == 1
    assert (reply.text, reply.call_sids) == (_CHECK_FAILED_TEXT, ["CA9"])
    assert (reply.budget_check_failed, reply.budget_reached) == (True, False)  # no budget was reached: no 402
    session = await session_manager.get_or_create("user", "chat")
    assert (session.messages[-1].role.value, session.messages[-1].content) == ("assistant", _CHECK_FAILED_MARKER)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


async def test_a_web_chat_turn_that_loses_the_budget_after_a_dial_ends_with_done(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager
) -> None:
    settings, tracker = await budgeted(1.0)
    llm = _scripted_llm(_response(_dial_call()), _response())
    agent = Agent(settings, llm, session_manager, tracker, _dial_registry(then=lambda: _drop_cost_table(tmp_path)))
    conversation = str(uuid.uuid4())

    events = await _web_chat(agent, "call them", conversation_id=conversation)

    assert events == [
        ("tool", {"phase": "start", "name": "Using make_phone_call..."}),
        ("tool", {"phase": "done", "name": "make_phone_call", "call_sid": "CA9"}),
        ("done", {"text": "", "cost_usd": pytest.approx(_CALL_COST), "budget_warning": _CHECK_FAILED_TEXT}),
    ]
    assert len(llm.streams) == 1
    messages = await _conversation_messages(session_manager, conversation)
    assert (messages[-1].role.value, messages[-1].content) == ("assistant", _CHECK_FAILED_MARKER)


def _lookup_registry(then: Callable[[], Awaitable[None]]) -> Any:
    """A registry whose `lookup` runs `then` and places no call."""
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def lookup(context: dict[str, Any] | None = None) -> str:
        await then()
        return "found"

    registry.register(
        name="lookup", description="Looks up", handler=lookup, parameters={"type": "object", "properties": {}}
    )
    return registry


@pytest.mark.parametrize("endpoint", ["message", "stream"])
async def test_a_chat_turn_that_loses_the_budget_mid_way_is_no_payment_required_answer(  # type: ignore[no-untyped-def]
    endpoint, tmp_path, budgeted, session_manager
) -> None:
    """The budget was never reached, so no 402: POST is a 503, the stream an `error` (lead ruling)."""
    settings, tracker = await budgeted(1.0)
    llm = _scripted_llm(_response([ToolCall(id="tc1", name="lookup", arguments={})]), _response())
    agent = Agent(settings, llm, session_manager, tracker, _lookup_registry(lambda: _drop_cost_table(tmp_path)))
    conversation = str(uuid.uuid4())

    if endpoint == "message":
        response = await _post_message(agent, "look it up", conversation_id=conversation)
        assert (response.status_code, response.json()) == (503, {"detail": _CHECK_FAILED_TEXT})
    else:
        events = await _web_chat(agent, "look it up", conversation_id=conversation)
        assert events[-1] == ("error", {"message": _CHECK_FAILED_TEXT, "cost_usd": pytest.approx(_CALL_COST)})
    messages = await _conversation_messages(session_manager, conversation)
    assert (messages[-1].role.value, messages[-1].content) == ("assistant", _CHECK_FAILED_MARKER)


_HEADLESS_CHECK_FAILED_TEXT = "Warning: Could not check the daily budget. Could not complete this scheduled action."


async def test_a_headless_run_whose_budget_cannot_be_read_makes_no_llm_call(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager, tool_registry, caplog
) -> None:
    """A scheduled action refuses like the chat paths, in its own words, instead of raising."""
    settings, tracker = await budgeted(1.0)
    await _drop_cost_table(tmp_path)
    llm = _scripted_llm(_response())
    agent = Agent(settings, llm, session_manager, tracker, tool_registry)

    with caplog.at_level("ERROR", logger="pincer.core.agent"):
        result = await agent.run_headless("hi", user_id="user", channel="scheduled")

    assert result == _HEADLESS_CHECK_FAILED_TEXT
    assert llm.complete.await_count == 0
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


async def test_a_headless_run_that_loses_the_budget_mid_way_stops_before_its_next_request(  # type: ignore[no-untyped-def]
    tmp_path, budgeted, session_manager
) -> None:
    settings, tracker = await budgeted(1.0)
    llm = _scripted_llm(_response([ToolCall(id="tc1", name="lookup", arguments={})]), _response())
    agent = Agent(settings, llm, session_manager, tracker, _lookup_registry(lambda: _drop_cost_table(tmp_path)))

    result = await agent.run_headless("look it up", user_id="user", channel="scheduled")

    assert result == _HEADLESS_CHECK_FAILED_TEXT
    assert llm.complete.await_count == 1


@pytest.mark.parametrize("endpoint", ["message", "stream"])
async def test_a_budget_stop_after_a_call_returns_the_turn_s_text_and_the_warning_apart(  # type: ignore[no-untyped-def]
    endpoint, tmp_path, budgeted, session_manager
) -> None:
    """A client that shows `done.text` (or `reply`) keeps "Calling Bob now.", and gets the warning
    beside it, the same on both endpoints (PR #366 review)."""
    settings, tracker = await budgeted(_CALL_COST)  # the dialling request crosses it
    calling = LLMResponse(
        content="Calling Bob now.",
        tool_calls=_dial_call(),
        request_model=_MODEL,
        stop_reason="tool_use",
        input_tokens=_CALL.input_tokens,
    )
    llm = _scripted_llm(calling, _response())
    agent = Agent(settings, llm, session_manager, tracker, _dial_registry())

    if endpoint == "message":
        response = await _post_message(agent, "call Bob")
        assert response.status_code == 200
        body = response.json()
        assert (body["reply"], body["budget_warning"], body["call_sids"]) == (
            "Calling Bob now.",
            _budget_text(_CALL_COST),
            ["CA9"],
        )
    else:
        events = await _web_chat(agent, "call Bob")
        assert events[-1] == (
            "done",
            {
                "text": "Calling Bob now.",
                "cost_usd": pytest.approx(_CALL_COST),
                "budget_warning": _budget_text(_CALL_COST),
            },
        )
