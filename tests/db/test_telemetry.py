"""Telemetry writes on the repository/service layer, on SQLite and Postgres.

The upsert rules here are what keep a call row honest while several
independent writers — the recorder, a turn finishing, a late Twilio callback —
touch it: counters add up, and a terminal status cannot be walked back.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from pincer.db.engine import get_engine
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.services.telemetry import TelemetryService
from pincer.voice.telemetry import queries, store

_TABLES = [TelephonyCall.__table__, TelephonyEvent.__table__, TelephonySpan.__table__, TelephonyTurn.__table__]

EARLIER = "2026-09-01T10:00:00+00:00"
LATER = "2026-09-01T10:05:00+00:00"

_EVENT = {
    "event_id": "e0",
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

_SPAN = {
    "span_id": "s0",
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
    """One row, read the way the read path reads it.

    `SELECT *` through `text()` has no result type, so Postgres hands back a
    naive `datetime` for a `TIMESTAMP` column where SQLite hands back the ISO
    string. Normalising here is what lets one assertion hold on both.
    """
    engine = get_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(f"SELECT * FROM {table} WHERE {key_column} = :key"),  # noqa: S608 - test fixture
            {"key": key},
        )
        row = result.mappings().one()
    return {
        column: value.replace(tzinfo=UTC).isoformat() if isinstance(value, datetime) else value
        for column, value in row.items()
    }


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
    assert row["registered_at"] == EARLIER  # written once, not re-stamped


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
    # The rewrite claims a different start; only the closing facts may move.
    await service.write_records(
        [],
        [{**span, "start_utc": LATER, "start_mono_ns": 99, "end_utc": LATER, "duration_ms": 12.5, "status": "error"}],
    )

    row = await _row(url, "telephony_spans", "span_id", "s1")
    assert (row["duration_ms"], row["status"]) == (12.5, "error")
    # A span that reset its start would misreport its duration and its place
    # on the critical path.
    assert (row["start_utc"], row["start_mono_ns"]) == (EARLIER, 1)


async def test_a_real_monotonic_clock_reading_fits(url):
    """`time.monotonic_ns()` outgrows a 32-bit column 2.15 seconds after boot.

    A too-narrow column does not lose precision here, it refuses the write —
    and the recorder swallows that, so the timeline would just stay empty.
    """
    service = TelemetryService(url)
    mono = time.monotonic_ns()
    assert mono > 2**31  # the value any host past its first seconds produces
    await service.write_records(
        [{**_EVENT, "event_id": "e1", "mono_ns": mono}],
        [{**_SPAN, "span_id": "s1", "start_mono_ns": mono, "end_mono_ns": mono + 1}],
    )

    assert (await _row(url, "telephony_events", "event_id", "e1"))["mono_ns"] == mono
    assert (await _row(url, "telephony_spans", "span_id", "s1"))["end_mono_ns"] == mono + 1


async def test_a_windowed_read_and_its_timestamps_survive_the_round_trip(url):
    """The read path is raw SQL, so it has no column to take a type from.

    Untyped, an ISO string goes straight at a `TIMESTAMP` column — which
    Postgres refuses — and a timestamp read back comes home as a `datetime`
    where every caller and the dashboard expect the ISO string SQLite returns.
    """
    service = TelemetryService(url)
    await service.upsert_call("c1", {"registered_at": EARLIER, "status": "completed"})

    engine = get_engine(url)
    async with engine.connect() as conn:
        rows = await store.fetch(conn, "SELECT * FROM telephony_calls WHERE registered_at >= ?", [EARLIER])
        assert [row["call_id"] for row in rows] == ["c1"]
        assert rows[0]["registered_at"] == EARLIER
        # A window that starts later excludes it — the comparison is a real
        # timestamp comparison, not a string one that happens to sort.
        assert await store.fetch(conn, "SELECT * FROM telephony_calls WHERE registered_at >= ?", [LATER]) == []


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


# ── the read paths, on both dialects ─────────────────────────────────


async def test_the_windowed_read_paths_run_on_both_dialects(migrated_url, monkeypatch):
    """`queries` and `alerts` are the legacy `?`-parameterised SQL.

    Every one of them windows on a timestamp, and they had no coverage on
    Postgres at all — where an untyped ISO string at a `TIMESTAMP` column takes
    down the overview, the call table and every alert at once.
    """
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call("c1", {"registered_at": now, "status": "completed", "direction": "inbound"})
    await service.upsert_turn(
        "t1", {"call_id": "c1", "turn_no": 1, "created_at": now, "total_ms": 900.0, "response_latency_ms": 640.0}
    )

    unused = Path("unused.db")  # the URL above decides the database
    filters = queries.CallFilters.for_hours(24)

    aggregate = await queries.overview(unused, filters)
    assert aggregate.calls["total"] == 1

    table = await queries.search_calls(unused, filters)
    assert [row["call_id"] for row in table["calls"]] == ["c1"]
    assert table["calls"][0]["registered_at"] == now  # an ISO string, not a datetime

    assert [turn["turn_id"] for turn in await queries.slowest_turns(unused, filters)] == ["t1"]


# ── the `?` rewriter ─────────────────────────────────────────────────


def test_positional_placeholders_become_named_ones_in_order():
    statement, values = store._named("SELECT * FROM t WHERE a = ? AND b > ?", ["x", 3])
    assert statement == "SELECT * FROM t WHERE a = :p0 AND b > :p1"
    assert values == {"p0": "x", "p1": 3}
    assert store._named("SELECT 1", []) == ("SELECT 1", {})


def test_a_placeholder_count_that_does_not_match_is_refused():
    """Loudly, rather than binding the wrong value to the wrong column.

    The rewriter reads the SQL character by character, so it also counts a `?`
    that is not a placeholder — a quoted literal, or Postgres' JSONB `?`
    operator. Such a query has to be written with a named parameter instead;
    this is what stops one from silently mis-binding.
    """
    with pytest.raises(ValueError, match="2 placeholders for 1 parameters"):
        store._named("SELECT * FROM t WHERE a = ? AND b = ?", ["only-one"])
    with pytest.raises(ValueError, match="1 placeholders for 0 parameters"):
        store._named("SELECT * FROM t WHERE name = 'what?'", [])
