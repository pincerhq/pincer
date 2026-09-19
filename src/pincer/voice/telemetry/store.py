"""
SQLite persistence for telephony telemetry.

Schema is owned by Alembic revision 0010; this module only reads and writes.
It follows the same convention as the rest of the voice subsystem: DDL through
migrations, queries through `aiosqlite` directly.

Three properties the write path must have, because the data arrives from an
unreliable world:

* **Idempotent.** Twilio retries status callbacks for minutes. Events carry a
  deterministic id for anything provider-sourced, and every insert is
  ``INSERT OR IGNORE`` / upsert — a duplicate is a no-op, not a second row on
  the timeline.
* **Order-independent.** Nothing depends on arrival order. Rows are ordered at
  read time by ``(ts_utc, seq)``; a late event lands in its right place.
* **Partial-tolerant.** A call row is written as soon as the call exists, and
  every later write is an upsert of the fields that are known. A call that dies
  mid-setup still has a row, its events, and whatever spans closed — which is
  precisely when someone needs them.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from pincer.voice.telemetry.records import TelemetryEvent, TelemetrySpan

logger = logging.getLogger(__name__)

_EVENT_COLUMNS = (
    "event_id, call_id, provider_call_id, trace_id, span_id, turn_id, name, ts_utc, mono_ns, seq, attributes"
)
_SPAN_COLUMNS = (
    "span_id, call_id, trace_id, parent_span_id, turn_id, name, start_utc, end_utc, "
    "start_mono_ns, end_mono_ns, duration_ms, status, attempt, attributes"
)

CALL_FIELDS = (
    "provider_call_id",
    "trace_id",
    "direction",
    "provider",
    "engine",
    "transport",
    "codec",
    "sample_rate",
    "model",
    "language",
    "tenant_id",
    "environment",
    "app_version",
    "from_number_masked",
    "to_number_masked",
    "registered_at",
    "dialed_at",
    "answered_at",
    "media_open_at",
    "ended_at",
    "status",
    "outcome",
    "failure_category",
    "termination_reason",
    "failure_code",
    "duration_ms",
    "setup_ms",
    "media_establish_ms",
    "turn_count",
    "tool_count",
    "error_count",
    "timeout_count",
    "retry_count",
    "interruption_count",
    "reconnect_count",
    "sampled",
    "sample_rate_used",
    "coverage",
    "config_json",
    "updated_at",
)

TURN_FIELDS = (
    "call_id",
    "turn_no",
    "trigger",
    "started_at",
    "first_audio_at",
    "engine",
    "model",
    "language",
    "streamed",
    "response_latency_ms",
    "response_latency_source",
    "endpointing_ms",
    "stt_first_partial_ms",
    "stt_final_ms",
    "agent_queue_ms",
    "agent_prep_ms",
    "llm_ttft_ms",
    "llm_total_ms",
    "tool_total_ms",
    "tts_first_audio_ms",
    "tts_total_ms",
    "audio_queue_ms",
    "total_ms",
    "tool_calls",
    "tool_retries",
    "tool_timeouts",
    "interrupted",
    "cancelled",
    "error",
    "bottleneck_stage",
    "bottleneck_ms",
    "critical_path",
    "complete",
    "created_at",
)

#: Counters an upsert ADDS to rather than replaces. A call accumulates errors
#: and interruptions across many independent writes; a plain overwrite would
#: leave the last writer's view of a running total.
_ACCUMULATING = frozenset(
    {
        "turn_count",
        "tool_count",
        "error_count",
        "timeout_count",
        "retry_count",
        "interruption_count",
        "reconnect_count",
    }
)

#: The only statuses a call row may be moved OUT of. Everything else — the
#: statuses `CallTracer.finish` writes: "completed", "failed", "ended" — is
#: terminal and ABSORBING.
#:
#: Stated as the live set rather than the terminal set on purpose: a terminal
#: status added later is then absorbing by default, whereas a forgotten entry
#: in a terminal list would silently re-open the hole. The live set is written
#: in exactly two places (`CallTracer.registered` and `.answered`).
#:
#: Why this lives in the UPDATE and not in a Python guard: Twilio retries
#: status callbacks for minutes, and by the time a late one lands the original
#: `CallTracer` may have been evicted (`runtime._MAX_TRACERS`, a 900 s context
#: TTL) or the process restarted. A caller then builds a FRESH tracer whose
#: `_finished` is False — see `hooks.call_declined` — and an in-memory flag
#: cannot see the terminal write the previous instance made. The row can.
LIVE_STATUSES = ("active", "connected")

_STATUS_ASSIGNMENT = "status=CASE WHEN COALESCE(telephony_calls.status,'') IN ('', {live}) THEN excluded.status ELSE telephony_calls.status END".format(  # noqa: E501
    live=", ".join(f"'{s}'" for s in LIVE_STATUSES)
)


def _assignment(column: str) -> str:
    """The `DO UPDATE SET` clause for one column."""
    if column in _ACCUMULATING:
        return f"{column}=telephony_calls.{column}+excluded.{column}"
    if column == "status":
        # COALESCE covers the row `dial_requested()` can create before anything
        # has set a status at all: NULL is not terminal, it is "not yet known".
        return _STATUS_ASSIGNMENT
    return f"{column}=excluded.{column}"


async def open_connection(db_path: str | Path) -> aiosqlite.Connection:
    """An open connection with row access by name. Caller closes it.

    The connection's worker thread is marked daemon: an aiosqlite connection is
    a non-daemon thread by default, so one that is never closed (a crash path, a
    test that forgets teardown) keeps the whole interpreter alive at exit.
    Telemetry must never be the reason a process refuses to shut down.
    """
    connection = aiosqlite.connect(str(db_path))
    _mark_daemon(connection)
    db = await connection
    db.row_factory = aiosqlite.Row
    return db


def _mark_daemon(connection: aiosqlite.Connection) -> None:
    """Best effort: aiosqlite's worker thread is private and version-dependent.

    Failing to mark it is harmless (a closed connection ends its thread anyway),
    so this never raises — it only removes the shutdown hang when a connection
    is leaked.
    """
    worker = getattr(connection, "_thread", None)
    if worker is not None:
        worker.daemon = True
    elif hasattr(connection, "daemon"):  # older aiosqlite: Connection IS a Thread
        connection.daemon = True  # type: ignore[attr-defined]


@asynccontextmanager
async def connect(db_path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    """Scoped connection.

    aiosqlite connections own a non-daemon thread, so an unclosed one keeps the
    interpreter alive at shutdown. Everything on the read path goes through this
    rather than holding a connection open.
    """
    db = await open_connection(db_path)
    try:
        yield db
    finally:
        await db.close()


async def ensure_schema(db_path: str | Path) -> None:
    """Bring the database to head so the telemetry tables exist."""
    import asyncio
    from pathlib import Path as _Path

    from pincer.db import ensure_schema_current

    await asyncio.to_thread(ensure_schema_current, _Path(str(db_path)))


async def tables_present(db: aiosqlite.Connection) -> bool:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('telephony_calls','telephony_events','telephony_spans','telephony_turns')"
    )
    return len(rows) == 4


class SqliteSink:
    """The recorder's durable sink. One connection, opened lazily, reused."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await open_connection(self._db_path)
            # The audio path never waits on us, but a long-running writer
            # blocking on another process's transaction would still stall the
            # queue behind it into a drop.
            await self._db.execute("PRAGMA busy_timeout = 2000")
        return self._db

    async def write(self, events: Sequence[TelemetryEvent], spans: Sequence[TelemetrySpan]) -> None:
        if not events and not spans:
            return
        db = await self._conn()
        if events:
            await db.executemany(
                f"INSERT OR IGNORE INTO telephony_events ({_EVENT_COLUMNS}) "  # noqa: S608 - constant columns
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [e.to_row() for e in events],
            )
        if spans:
            # A span is written once when it closes, but a retried write (or a
            # span closed twice by a cancellation race) must not duplicate.
            await db.executemany(
                f"INSERT INTO telephony_spans ({_SPAN_COLUMNS}) "  # noqa: S608 - constant columns
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(span_id) DO UPDATE SET "
                "end_utc=excluded.end_utc, end_mono_ns=excluded.end_mono_ns, "
                "duration_ms=excluded.duration_ms, status=excluded.status, attributes=excluded.attributes",
                [s.to_row() for s in spans],
            )
        await db.commit()

    async def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            try:
                await db.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("telemetry sink close failed", exc_info=True)


# ── call + turn upserts (not on the audio path) ──────────────────────


async def upsert_call(db_path: str | Path, call_id: str, **fields: Any) -> None:
    """Create or update the per-call row with whatever is known right now."""
    known = {k: v for k, v in fields.items() if k in CALL_FIELDS and v is not None}
    if not known:
        return
    async with connect(db_path) as db:
        await _upsert_call(db, call_id, known)
        await db.commit()


async def _upsert_call(db: aiosqlite.Connection, call_id: str, known: dict[str, Any]) -> None:
    columns = ", ".join(known)
    placeholders = ", ".join("?" for _ in known)
    assignments = ", ".join(_assignment(k) for k in known)
    await db.execute(
        f"INSERT INTO telephony_calls (call_id, {columns}) VALUES (?, {placeholders}) "  # noqa: S608
        f"ON CONFLICT(call_id) DO UPDATE SET {assignments}",
        (call_id, *known.values()),
    )


async def save_turn(db_path: str | Path, turn_id: str, **fields: Any) -> None:
    """Write a completed turn's derived latencies.

    Upsert rather than insert: a turn that was cancelled by barge-in is written
    once when it is cancelled and the row must not be duplicated if a late
    completion also fires.
    """
    known = {k: v for k, v in fields.items() if k in TURN_FIELDS and v is not None}
    if not known:
        return
    columns = ", ".join(known)
    placeholders = ", ".join("?" for _ in known)
    assignments = ", ".join(f"{k}=excluded.{k}" for k in known)
    async with connect(db_path) as db:
        await db.execute(
            f"INSERT INTO telephony_turns (turn_id, {columns}) VALUES (?, {placeholders}) "  # noqa: S608
            f"ON CONFLICT(turn_id) DO UPDATE SET {assignments}",
            (turn_id, *known.values()),
        )
        await db.commit()


def loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
