"""
Persistence for telephony telemetry.

Schema is owned by Alembic revisions 0010 and 0016; this module only reads and
writes. Writes go through `pincer.services.telemetry`; the reads here are the
legacy `?`-parameterised queries, run on the shared engine so they work on both
dialects.

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
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import bindparam, text

from pincer.db.engine import get_database_url, get_engine
from pincer.db.types import IsoText, Uuid7
from pincer.repositories.telemetry import EVENT_COLUMNS, SPAN_COLUMNS
from pincer.services.telemetry import TelemetryService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy import BindParameter
    from sqlalchemy.ext.asyncio import AsyncConnection

    from pincer.voice.telemetry.records import TelemetryEvent, TelemetrySpan

logger = logging.getLogger(__name__)

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


#: An ISO-8601 timestamp, as the telemetry columns store it. Loose on purpose:
#: it only has to separate a timestamp from the other things these queries
#: compare — ids, names, statuses, `LIKE` needles (which carry `%`) and numbers.
_ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

_ISO_TEXT = IsoText()


_UUID7 = Uuid7()


def _bound(name: str, value: Any) -> BindParameter[Any]:
    """One parameter, typed for the column it is compared against.

    The write path binds each parameter to its model column's type; a read
    written as raw SQL has no column to take one from, and on Postgres an
    untyped value at a typed column is refused outright.

    Timestamps are recognised by shape, which is safe: these queries compare
    an ISO string against nothing but a timestamp column.

    **Ids are not.** The caller passes a `uuid.UUID` to mean "compare this
    against a uuid column", because shape alone cannot decide it — the same
    string is compared against `call_id` (uuid) and `provider_call_id` (text)
    in one clause, a uuid-shaped `tenant_id` belongs to a text column, and a
    `LIKE` needle must stay text whatever it looks like. Guessing got all
    three wrong.
    """
    if isinstance(value, str) and _ISO_STAMP.match(value):
        return bindparam(name, value, type_=_ISO_TEXT)
    if isinstance(value, UUID):
        return bindparam(name, str(value), type_=_UUID7)
    return bindparam(name, value)


async def fetch(db: AsyncConnection, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    """Run one read, returning rows addressable by column name.

    Values come back in the form every caller and the dashboard expect, on
    both dialects: `SELECT *` through `text()` has no result type, so Postgres
    would otherwise hand back a naive `datetime` for a `TIMESTAMP` column and a
    `uuid.UUID` for a `uuid` one. The first would be read as local time by the
    browser; the second would be serialised as a repr.
    """
    statement, values = _named(sql, params)
    stmt = text(statement).bindparams(*(_bound(name, value) for name, value in values.items()))
    result = await db.execute(stmt)
    return [{column: _as_python(value, db.dialect) for column, value in row.items()} for row in result.mappings().all()]


def _as_python(value: Any, dialect: Any) -> Any:
    if isinstance(value, datetime):
        return _ISO_TEXT.process_result_value(value, dialect)
    if isinstance(value, UUID):
        return str(value)
    return value


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
