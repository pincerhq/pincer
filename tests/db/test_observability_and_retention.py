"""Observability storage and the retention purge, on SQLite and Postgres."""

from __future__ import annotations

import pytest

from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.models.observability import AppointmentOutcome, CallCost, CanaryRun
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.models.voice import CallAction, CallAnalytics, CallTranscript, InboundMessage, OutboundCallLog, VoiceCall
from pincer.repositories.base import BaseRepository
from pincer.services.observability import BookingsService, CallCostsService, CanaryService
from pincer.services.retention import RetentionService

_TABLES = [
    model.__table__
    for model in (
        AppointmentOutcome,
        CallCost,
        CanaryRun,
        VoiceCall,
        CallTranscript,
        CallAction,
        InboundMessage,
        OutboundCallLog,
        CallAnalytics,
        TelephonyCall,
        TelephonyEvent,
        TelephonySpan,
        TelephonyTurn,
    )
]

OLD = "2020-01-01T00:00:00+00:00"
RECENT = "2026-09-01T00:00:00+00:00"
CUTOFF = "2026-01-01T00:00:00+00:00"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: VoiceCall.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))


async def _add(url: str, *rows) -> None:
    async with session_scope(url) as session:
        session.add_all(list(rows))


async def _count(url: str, model) -> int:
    async with session_scope(url) as session:
        repo: BaseRepository = BaseRepository(session)
        repo.model = model
        return len(await repo.list())


# ── bookings ─────────────────────────────────────────────────────────


async def test_a_booking_outcome_is_one_row_per_task(url):
    """A task can span several dial attempts and must count once."""
    service = BookingsService(url)
    base = {"task_id": "task-1", "language": "de", "detail": "", "recorded_at": RECENT}
    await service.record_outcome({**base, "result": "unreachable", "call_sid": "CA1", "attempts": 1})
    await service.record_outcome({**base, "result": "confirmed", "call_sid": "CA2", "attempts": 2})

    assert await service.counts_by_result(CUTOFF) == {"confirmed": 1}
    async with session_scope(url) as session:
        repo: BaseRepository = BaseRepository(session)
        repo.model = AppointmentOutcome
        (row,) = await repo.list()
    assert (row.call_sid, row.attempts) == ("CA2", 2)


async def test_booking_counts_respect_the_window(url):
    service = BookingsService(url)
    await service.record_outcome({"task_id": "old", "result": "confirmed", "attempts": 1, "recorded_at": OLD})
    await service.record_outcome({"task_id": "new", "result": "declined", "attempts": 1, "recorded_at": RECENT})
    assert await service.counts_by_result(CUTOFF) == {"declined": 1}


# ── call costs ───────────────────────────────────────────────────────


async def test_a_call_cost_is_replaced_when_re_priced(url):
    service = CallCostsService(url)
    await service.save({"call_sid": "CA1", "engine": "media_streams", "total_usd": 1.0, "recorded_at": RECENT})
    await service.save({"call_sid": "CA1", "engine": "media_streams", "total_usd": 2.5, "recorded_at": RECENT})

    stored = await service.get("CA1")
    assert stored["total_usd"] == pytest.approx(2.5)
    assert await service.get("CA-unknown") is None


async def test_costs_for_a_page_of_calls_in_one_query(url):
    service = CallCostsService(url)
    for sid, total in (("CA1", 1.0), ("CA2", 2.0), ("CA3", 3.0)):
        await service.save({"call_sid": sid, "total_usd": total, "recorded_at": RECENT})

    assert await service.totals_for(["CA1", "CA3", "CA-missing"]) == {"CA1": 1.0, "CA3": 3.0}
    assert await service.totals_for([]) == {}


# ── canary ───────────────────────────────────────────────────────────


async def test_canary_runs_come_back_newest_first(url):
    service = CanaryService(url)
    for ran_at, ok in (("2026-09-01T00:00:00+00:00", 1), ("2026-09-03T00:00:00+00:00", 0)):
        await service.record_run(
            {"ran_at": ran_at, "ok": ok, "skipped": 0, "reason": "", "call_sid": "", "turns": 2, "duration_s": 1.5}
        )

    runs = await service.recent(limit=10)
    assert [run["ran_at"] for run in runs] == ["2026-09-03T00:00:00+00:00", "2026-09-01T00:00:00+00:00"]
    assert runs[0]["ok"] == 0
    assert len(await service.recent(limit=1)) == 1


# ── retention ────────────────────────────────────────────────────────


async def test_the_voice_purge_deletes_expired_rows_and_keeps_the_rest(url):
    await _add(
        url,
        VoiceCall(call_sid="CA_old", started_at=OLD),
        VoiceCall(call_sid="CA_new", started_at=RECENT),
        CallTranscript(call_id="CA_old", speaker="agent", text="hello", timestamp=OLD),
        CallTranscript(call_id="CA_new", speaker="agent", text="hello", timestamp=RECENT),
        CallAction(call_id="CA_old", action_type="tool_call", timestamp=OLD),
        InboundMessage(call_sid="CA_old", created_at=OLD),
        OutboundCallLog(phone_number="+4930111", placed_at=OLD, local_day="2020-01-01"),
    )

    deleted = await RetentionService(url).purge_voice(CUTOFF)

    assert deleted == {
        "voice_calls": 1,
        "call_transcripts": 1,
        "call_actions": 1,
        "inbound_messages": 1,
        "outbound_call_logs": 1,
    }
    assert await _count(url, VoiceCall) == 1
    assert await _count(url, CallTranscript) == 1


async def test_the_purge_reports_nothing_when_nothing_expired(url):
    await _add(url, VoiceCall(call_sid="CA_new", started_at=RECENT))
    assert await RetentionService(url).purge_voice(CUTOFF) == {}


async def test_an_analytics_row_survives_but_its_rationale_does_not(url):
    """The derived numbers outlive the transcript; a sentence quoting the call does not."""
    await _add(
        url,
        VoiceCall(call_sid="CA_old", started_at=OLD),
        VoiceCall(call_sid="CA_new", started_at=RECENT),
        CallAnalytics(
            call_sid="CA_old",
            method="exact",
            created_at=RECENT,  # analysed recently, but about an expired call
            sentiment="negative",
            sentiment_rationale="said the third delay was unacceptable",
        ),
        CallAnalytics(call_sid="CA_new", method="exact", created_at=RECENT, sentiment_rationale="fine"),
    )

    deleted = await RetentionService(url).purge_voice(CUTOFF)

    assert deleted["call_analytics.sentiment_rationale"] == 1
    assert await _count(url, CallAnalytics) == 2  # both rows survive
    async with session_scope(url) as session:
        repo: BaseRepository = BaseRepository(session)
        repo.model = CallAnalytics
        rationales = {row.call_sid: row.sentiment_rationale for row in await repo.list()}
    assert rationales == {"CA_old": None, "CA_new": "fine"}


async def test_the_telemetry_purge_has_its_own_window(url):
    await _add(
        url,
        TelephonyCall(call_id="c_old", registered_at=OLD),
        TelephonyCall(call_id="c_new", registered_at=RECENT),
        TelephonyEvent(event_id="e1", call_id="c_old", name="dial", ts_utc=OLD),
        TelephonySpan(span_id="s1", call_id="c_old", name="llm", start_utc=OLD),
        TelephonyTurn(turn_id="t1", call_id="c_old", created_at=OLD),
    )

    deleted = await RetentionService(url).purge_telemetry(CUTOFF)

    assert deleted == {
        "telephony_events": 1,
        "telephony_spans": 1,
        "telephony_turns": 1,
        "telephony_calls": 1,
    }
    assert await _count(url, TelephonyCall) == 1
