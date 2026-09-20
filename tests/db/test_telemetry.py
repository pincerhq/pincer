"""Telemetry writes on the repository/service layer, on SQLite and Postgres.

The upsert rules here are what keep a call row honest while several
independent writers — the recorder, a turn finishing, a late Twilio callback —
touch it: counters add up, and a terminal status cannot be walked back.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from pincer.db.engine import get_engine
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.services.telemetry import TelemetryService

_TABLES = [TelephonyCall.__table__, TelephonyEvent.__table__, TelephonySpan.__table__, TelephonyTurn.__table__]

EARLIER = "2026-09-01T10:00:00+00:00"
LATER = "2026-09-01T10:05:00+00:00"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: TelephonyCall.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: TelephonyCall.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: TelephonyCall.metadata.drop_all(c, tables=_TABLES))


async def _row(url: str, table: str, key_column: str, key: str) -> dict:
    engine = get_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(f"SELECT * FROM {table} WHERE {key_column} = :key"),  # noqa: S608 - test fixture
            {"key": key},
        )
        return dict(result.mappings().one())


# ── the call row ─────────────────────────────────────────────────────


async def test_counters_accumulate_across_independent_writes(url):
    """Each turn adds one; a writer that overwrote would lose the others."""
    service = TelemetryService(url)
    await service.upsert_call("c1", {"registered_at": EARLIER, "turn_count": 1, "error_count": 1})
    await service.upsert_call("c1", {"turn_count": 1})
    await service.upsert_call("c1", {"turn_count": 1, "error_count": 2})

    row = await _row(url, "telephony_calls", "call_id", "c1")
    assert row["turn_count"] == 3
    assert row["error_count"] == 3
    assert row["registered_at"] is not None


async def test_everything_else_takes_the_incoming_value(url):
    service = TelemetryService(url)
    await service.upsert_call("c1", {"engine": "media_streams", "language": "de"})
    await service.upsert_call("c1", {"language": "en"})

    row = await _row(url, "telephony_calls", "call_id", "c1")
    assert (row["engine"], row["language"]) == ("media_streams", "en")


async def test_a_terminal_status_absorbs_later_writes(url):
    """A late Twilio callback must not re-open a call that has ended."""
    service = TelemetryService(url)
    await service.upsert_call("c1", {"status": "active"})
    await service.upsert_call("c1", {"status": "connected"})
    await service.upsert_call("c1", {"status": "completed"})
    await service.upsert_call("c1", {"status": "active"})  # the late callback

    assert (await _row(url, "telephony_calls", "call_id", "c1"))["status"] == "completed"


async def test_a_row_with_no_status_yet_accepts_one(url):
    """`dial_requested` can create the row before anything knows a status."""
    service = TelemetryService(url)
    await service.upsert_call("c1", {"registered_at": EARLIER})
    await service.upsert_call("c1", {"status": "active"})
    assert (await _row(url, "telephony_calls", "call_id", "c1"))["status"] == "active"


async def test_nothing_known_writes_nothing(url):
    service = TelemetryService(url)
    await service.upsert_call("c1", {})
    engine = get_engine(url)
    async with engine.connect() as conn:
        count = (await conn.execute(sa.text("SELECT COUNT(*) FROM telephony_calls"))).scalar_one()
    assert count == 0


# ── turns, events and spans ──────────────────────────────────────────


async def test_a_turn_is_written_once_however_often_it_is_reported(url):
    """A turn cancelled by barge-in is written when it is cancelled; a late
    completion updates that row rather than adding another."""
    service = TelemetryService(url)
    await service.upsert_turn("t1", {"call_id": "c1", "turn_no": 1, "cancelled": 1, "created_at": EARLIER})
    await service.upsert_turn(
        "t1", {"call_id": "c1", "turn_no": 1, "cancelled": 0, "total_ms": 900.0, "created_at": EARLIER}
    )

    row = await _row(url, "telephony_turns", "turn_id", "t1")
    assert (row["cancelled"], row["total_ms"]) == (0, 900.0)


async def test_a_repeated_event_is_ignored_not_duplicated(url):
    service = TelemetryService(url)
    event = {
        "event_id": "e1",
        "call_id": "c1",
        "provider_call_id": "",
        "trace_id": "",
        "span_id": "",
        "turn_id": "",
        "name": "call.registered",
        "ts_utc": EARLIER,
        "mono_ns": 1,
        "seq": 1,
        "attributes": "{}",
    }
    await service.write_records([event], [])
    await service.write_records([{**event, "name": "changed"}], [])

    row = await _row(url, "telephony_events", "event_id", "e1")
    assert row["name"] == "call.registered"  # the first write stands


async def test_a_span_rewrite_updates_its_close(url):
    service = TelemetryService(url)
    span = {
        "span_id": "s1",
        "call_id": "c1",
        "trace_id": "",
        "parent_span_id": "",
        "turn_id": "",
        "name": "llm",
        "start_utc": EARLIER,
        "end_utc": None,
        "start_mono_ns": 1,
        "end_mono_ns": None,
        "duration_ms": None,
        "status": "ok",
        "attempt": 1,
        "attributes": "{}",
    }
    await service.write_records([], [span])
    await service.write_records([], [{**span, "end_utc": LATER, "duration_ms": 12.5, "status": "error"}])

    row = await _row(url, "telephony_spans", "span_id", "s1")
    assert (row["duration_ms"], row["status"]) == (12.5, "error")
    assert row["start_utc"] is not None  # the opening facts are untouched


async def test_events_and_spans_are_one_batch(url):
    service = TelemetryService(url)
    await service.write_records(
        [
            {
                "event_id": f"e{i}",
                "call_id": "c1",
                "provider_call_id": "",
                "trace_id": "",
                "span_id": "",
                "turn_id": "",
                "name": "tick",
                "ts_utc": EARLIER,
                "mono_ns": i,
                "seq": i,
                "attributes": "{}",
            }
            for i in range(3)
        ],
        [],
    )
    engine = get_engine(url)
    async with engine.connect() as conn:
        count = (await conn.execute(sa.text("SELECT COUNT(*) FROM telephony_events"))).scalar_one()
    assert count == 3
