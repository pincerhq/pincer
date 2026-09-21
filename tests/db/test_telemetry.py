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
from support import seeded_id

from pincer.db.engine import get_engine
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.services.telemetry import CallFilters, TelemetryService
from pincer.voice.telemetry import queries

_TABLES = [TelephonyCall.__table__, TelephonyEvent.__table__, TelephonySpan.__table__, TelephonyTurn.__table__]

CALL = seeded_id("call-1")
TURN = seeded_id("turn-1")

EARLIER = "2026-09-01T10:00:00+00:00"
LATER = "2026-09-01T10:05:00+00:00"

_EVENT = {
    "event_id": "e0",
    "call_id": CALL,
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
    "call_id": CALL,
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
    await service.upsert_call(CALL, {"registered_at": EARLIER, "turn_count": 1, "error_count": 1})
    await service.upsert_call(CALL, {"turn_count": 1})
    await service.upsert_call(CALL, {"turn_count": 1, "error_count": 2})

    row = await _row(url, "telephony_calls", "call_id", CALL)
    assert row["turn_count"] == 3
    assert row["error_count"] == 3
    assert row["registered_at"] == EARLIER  # written once, not re-stamped


async def test_everything_else_takes_the_incoming_value(url):
    service = TelemetryService(url)
    await service.upsert_call(CALL, {"engine": "media_streams", "language": "de"})
    await service.upsert_call(CALL, {"language": "en"})

    row = await _row(url, "telephony_calls", "call_id", CALL)
    assert (row["engine"], row["language"]) == ("media_streams", "en")


async def test_a_terminal_status_absorbs_later_writes(url):
    """A late Twilio callback must not re-open a call that has ended."""
    service = TelemetryService(url)
    await service.upsert_call(CALL, {"status": "active"})
    await service.upsert_call(CALL, {"status": "connected"})
    await service.upsert_call(CALL, {"status": "completed"})
    await service.upsert_call(CALL, {"status": "active"})  # the late callback

    assert (await _row(url, "telephony_calls", "call_id", CALL))["status"] == "completed"


async def test_a_row_with_no_status_yet_accepts_one(url):
    """`dial_requested` can create the row before anything knows a status."""
    service = TelemetryService(url)
    await service.upsert_call(CALL, {"registered_at": EARLIER})
    await service.upsert_call(CALL, {"status": "active"})
    assert (await _row(url, "telephony_calls", "call_id", CALL))["status"] == "active"


async def test_nothing_known_writes_nothing(url):
    service = TelemetryService(url)
    await service.upsert_call(CALL, {})
    engine = get_engine(url)
    async with engine.connect() as conn:
        count = (await conn.execute(sa.text("SELECT COUNT(*) FROM telephony_calls"))).scalar_one()
    assert count == 0


# ── turns, events and spans ──────────────────────────────────────────


async def test_a_turn_is_written_once_however_often_it_is_reported(url):
    """A turn cancelled by barge-in is written when it is cancelled; a late
    completion updates that row rather than adding another."""
    service = TelemetryService(url)
    await service.upsert_turn(TURN, {"call_id": CALL, "turn_no": 1, "cancelled": 1, "created_at": EARLIER})
    await service.upsert_turn(
        TURN, {"call_id": CALL, "turn_no": 1, "cancelled": 0, "total_ms": 900.0, "created_at": EARLIER}
    )

    row = await _row(url, "telephony_turns", "turn_id", TURN)
    assert (row["cancelled"], row["total_ms"]) == (0, 900.0)


async def test_a_repeated_event_is_ignored_not_duplicated(url):
    service = TelemetryService(url)
    event = {
        "event_id": "e1",
        "call_id": CALL,
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
        "call_id": CALL,
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
    """An ISO string binds at a `TIMESTAMP` column on Postgres, and a timestamp
    comes home as the ISO string SQLite returns, not a `datetime`."""
    service = TelemetryService(url)
    await service.upsert_call(CALL, {"registered_at": EARLIER, "status": "completed"})

    async with service.reads() as reads:
        assert reads is not None
        rows = await reads.calls(CallFilters(since=EARLIER).where(), limit=10)
        assert [row["call_id"] for row in rows] == [CALL]
        assert rows[0]["registered_at"] == EARLIER
        # A window that starts later excludes it — the comparison is a real
        # timestamp comparison, not a string one that happens to sort.
        assert await reads.calls(CallFilters(since=LATER).where(), limit=10) == []


async def test_events_and_spans_are_one_batch(url):
    service = TelemetryService(url)
    await service.write_records(
        [
            {
                "event_id": f"e{i}",
                "call_id": CALL,
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
    await service.upsert_call(CALL, {"registered_at": now, "status": "completed", "direction": "inbound"})
    await service.upsert_turn(
        TURN, {"call_id": CALL, "turn_no": 1, "created_at": now, "total_ms": 900.0, "response_latency_ms": 640.0}
    )

    unused = Path("unused.db")  # the URL above decides the database
    filters = queries.CallFilters.for_hours(24)

    aggregate = await queries.overview(unused, filters)
    assert aggregate.calls["total"] == 1

    table = await queries.search_calls(unused, filters)
    assert [row["call_id"] for row in table["calls"]] == [CALL]
    assert table["calls"][0]["registered_at"] == now  # an ISO string, not a datetime

    assert [turn["turn_id"] for turn in await queries.slowest_turns(unused, filters)] == [TURN]


async def test_a_call_is_found_by_either_of_its_two_names(migrated_url, monkeypatch):
    """`get_call` takes an internal id OR a provider CallSid, and compares one
    value against a uuid column and a text column in the same clause.

    Only one of those can be typed correctly for a given value, so the query
    has to stop asking — and on Postgres the wrong guess is not a miss, it is
    `operator does not exist`. This is the whole call-detail surface: the page,
    its timeline, its export, and `pincer telephony call`.
    """
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "provider_call_id": "CA_provider", "status": "completed"})
    await service.write_records([{**_EVENT, "event_id": "ev-1", "call_id": CALL, "ts_utc": now}], [])
    await service.upsert_turn(TURN, {"call_id": CALL, "turn_no": 1, "created_at": now})

    unused = Path("unused.db")
    by_id = await queries.get_call(unused, CALL)
    by_sid = await queries.get_call(unused, "CA_provider")
    assert by_id is not None and by_sid is not None
    assert by_id["call_id"] == by_sid["call_id"] == CALL

    # An id that names nothing is an empty answer, not an error — and one that
    # could never be an id at all must not reach the uuid column.
    assert await queries.get_call(unused, "CA_never_dialled") is None
    assert await queries.get_call(unused, "not-an-id-at-all") is None

    # The per-call timelines compare the same uuid column.
    assert [event["event_id"] for event in await queries.get_events(unused, CALL)] == ["ev-1"]
    assert [turn["turn_id"] for turn in await queries.get_turns(unused, CALL)] == [TURN]
    assert await queries.get_spans(unused, CALL) == []
    assert await queries.get_events(unused, "not-an-id-at-all") == []


async def test_the_call_search_box_matches_an_id_as_text(migrated_url, monkeypatch):
    """The search box `LIKE`s across `call_id`, and Postgres has no
    `uuid LIKE text` — so the column is cast rather than the needle typed."""
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "provider_call_id": "CA_provider"})

    unused = Path("unused.db")
    found = await queries.search_calls(unused, queries.CallFilters.for_hours(24, search=CALL[:8]))
    assert [row["call_id"] for row in found["calls"]] == [CALL]

    by_sid = await queries.search_calls(unused, queries.CallFilters.for_hours(24, search="CA_prov"))
    assert [row["call_id"] for row in by_sid["calls"]] == [CALL]


async def test_the_call_search_box_ignores_case_on_both_dialects(migrated_url, monkeypatch):
    """SQLite's LIKE ignores ASCII case and Postgres' does not, so one
    dialect used to find a call the other could not."""
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "provider_call_id": "CA_Provider"})

    unused = Path("unused.db")
    for needle in ("ca_prov", "CA_PROVIDER", CALL[:8].upper()):
        found = await queries.search_calls(unused, queries.CallFilters.for_hours(24, search=needle))
        assert [row["call_id"] for row in found["calls"]] == [CALL], needle


async def test_a_search_term_is_taken_literally(migrated_url, monkeypatch):
    """`%` and `_` in the box are characters, not wildcards."""
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "provider_call_id": "CAxprovider"})

    unused = Path("unused.db")
    for needle in ("CA_provider", "%"):
        found = await queries.search_calls(unused, queries.CallFilters.for_hours(24, search=needle))
        assert found["calls"] == [], needle


async def test_a_filter_that_looks_like_a_timestamp_is_still_compared_as_text(migrated_url, monkeypatch):
    """Filters come straight from query parameters. Typing a value by its
    shape sent `status=2026-13-45T00:00` to `datetime.fromisoformat` — a 500 on
    Postgres only — and a well-formed one at a text column as a timestamp."""
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "status": "completed"})

    unused = Path("unused.db")
    for status in ("2026-13-45T00:00", "2026-01-01T00:00"):
        filters = queries.CallFilters.for_hours(24, status=status)
        assert (await queries.search_calls(unused, filters))["calls"] == []
        assert (await queries.overview(unused, filters)).calls["total"] == 0


async def test_a_uuid_shaped_tenant_is_still_a_text_column(migrated_url, monkeypatch):
    """`tenant_id` is text, and a deployment may well name tenants with uuids.

    Typing a parameter by its shape sent that one at a text column; the header
    is caller-supplied, so it was a 500 anyone could trigger.
    """
    monkeypatch.setenv("PINCER_DATABASE_URL", migrated_url)
    tenant = seeded_id("tenant-1")
    service = TelemetryService(migrated_url)
    now = datetime.now(UTC).isoformat()
    await service.upsert_call(CALL, {"registered_at": now, "tenant_id": tenant})

    unused = Path("unused.db")
    scope = queries.TenantScope(allowed=(tenant,))
    found = await queries.search_calls(unused, queries.CallFilters.for_hours(24), scope=scope)
    assert [row["call_id"] for row in found["calls"]] == [CALL]
