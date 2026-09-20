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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from pincer.db.engine import get_database_url, get_engine
from pincer.repositories.telemetry import EVENT_COLUMNS, SPAN_COLUMNS
from pincer.services.telemetry import TelemetryService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncConnection

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


def _named(sql: str, params: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """Positional `?` placeholders as named ones.

    The read queries are written with `?`, which SQLite understands and
    Postgres does not. Numbering them keeps every query as it was while making
    it run on both.
    """
    out: list[str] = []
    index = 0
    for char in sql:
        if char == "?":
            out.append(f":p{index}")
            index += 1
        else:
            out.append(char)
    if index != len(params):
        raise ValueError(f"{index} placeholders for {len(params)} parameters")
    return "".join(out), {f"p{i}": value for i, value in enumerate(params)}


@asynccontextmanager
async def connect(db_path: str | Path) -> AsyncIterator[AsyncConnection]:
    """A connection for reading telemetry."""
    engine = get_engine(get_database_url(Path(str(db_path))))
    async with engine.connect() as conn:
        yield conn


async def fetch(db: AsyncConnection, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    """Run one read, returning rows addressable by column name."""
    statement, values = _named(sql, params)
    result = await db.execute(text(statement), values)
    return list(result.mappings().all())


async def tables_present(db: AsyncConnection) -> bool:
    """True once the telemetry tables exist — a fresh database has none."""

    def _check(sync_conn: Any) -> bool:
        from sqlalchemy import inspect

        names = set(inspect(sync_conn).get_table_names())
        return {"telephony_calls", "telephony_events", "telephony_spans", "telephony_turns"} <= names

    return bool(await db.run_sync(_check))


def _as_row(columns: Sequence[str], values: tuple[Any, ...]) -> dict[str, Any]:
    """A record's positional row as a column mapping."""
    return dict(zip(columns, values, strict=True))


class SqlSink:
    """The recorder's durable sink.

    A batch is one unit of work. Slow is survivable — the recorder queues and
    drops when it has to; what must not happen is the audio path waiting.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._service: TelemetryService | None = None

    async def _store(self) -> TelemetryService:
        if self._service is None:
            self._service = TelemetryService(get_database_url(Path(self._db_path)))
        return self._service

    async def write(self, events: Sequence[TelemetryEvent], spans: Sequence[TelemetrySpan]) -> None:
        if not events and not spans:
            return
        service = await self._store()
        await service.write_records(
            [_as_row(EVENT_COLUMNS, event.to_row()) for event in events],
            [_as_row(SPAN_COLUMNS, span.to_row()) for span in spans],
        )

    async def close(self) -> None:
        self._service = None


#: The name this sink had when it was SQLite-only.
SqliteSink = SqlSink


# ── call + turn upserts (not on the audio path) ──────────────────────


async def upsert_call(db_path: str | Path, call_id: str, **fields: Any) -> None:
    """Create or update the per-call row with whatever is known right now."""
    known = {k: v for k, v in fields.items() if k in CALL_FIELDS and v is not None}
    if not known:
        return
    await TelemetryService(get_database_url(Path(str(db_path)))).upsert_call(call_id, known)


async def save_turn(db_path: str | Path, turn_id: str, **fields: Any) -> None:
    """Write a completed turn's derived latencies."""
    known = {k: v for k, v in fields.items() if k in TURN_FIELDS and v is not None}
    if not known:
        return
    await TelemetryService(get_database_url(Path(str(db_path)))).upsert_turn(turn_id, known)


def loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
