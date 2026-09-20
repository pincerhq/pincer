"""Telephony telemetry: calls, the event timeline, spans and per-turn latencies.

Hand-written SQL, cached per column set, rather than statements built through
the query builder. These writes run on the same event loop as the audio path,
and everything done before the driver sees the statement is loop time: the
overhead test measures builder-per-write as ~7 ms of audio-loop delay at 25
concurrent calls, against ~0.7 ms for this.

It stays portable: `INSERT … ON CONFLICT (…) DO UPDATE/NOTHING` and named
parameters mean the same thing on SQLite and Postgres. `INSERT OR IGNORE`,
which does not, is written as `DO NOTHING`. Each parameter is bound to its
model column's type, so the timestamps that are TEXT on SQLite and TIMESTAMP
on Postgres convert either way — raw SQL alone would send a string to a
`timestamp` column and be refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, text

from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.sql import Executable

#: Counters an upsert ADDS to rather than replaces. A call accumulates errors
#: and interruptions across many independent writes; a plain overwrite would
#: leave the last writer's view of a running total.
ACCUMULATING = frozenset(
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
#: in a terminal list would silently re-open the hole.
#:
#: Why this lives in the UPDATE and not in a Python guard: Twilio retries
#: status callbacks for minutes, and by the time a late one lands the original
#: `CallTracer` may have been evicted or the process restarted. A caller then
#: builds a FRESH tracer whose `_finished` is False — see `hooks.call_declined`
#: — and an in-memory flag cannot see the terminal write the previous instance
#: made. The row can.
LIVE_STATUSES = ("active", "connected")

_STATUS_ASSIGNMENT = (
    "status=CASE WHEN COALESCE(telephony_calls.status,'') IN ('', {live}) "
    "THEN excluded.status ELSE telephony_calls.status END"
).format(live=", ".join(f"'{status}'" for status in LIVE_STATUSES))

#: Span columns a re-write may change. A span is written once when it closes,
#: but a retried write (or a span closed twice by a cancellation race) must
#: update rather than duplicate.
_SPAN_UPDATES = ("end_utc", "end_mono_ns", "duration_ms", "status", "attributes")

EVENT_COLUMNS = (
    "event_id",
    "call_id",
    "provider_call_id",
    "trace_id",
    "span_id",
    "turn_id",
    "name",
    "ts_utc",
    "mono_ns",
    "seq",
    "attributes",
)

SPAN_COLUMNS = (
    "span_id",
    "call_id",
    "trace_id",
    "parent_span_id",
    "turn_id",
    "name",
    "start_utc",
    "end_utc",
    "start_mono_ns",
    "end_mono_ns",
    "duration_ms",
    "status",
    "attempt",
    "attributes",
)


def _call_assignment(column: str) -> str:
    """The `DO UPDATE SET` clause for one column of the call row."""
    if column in ACCUMULATING:
        return f"{column}=telephony_calls.{column}+excluded.{column}"
    if column == "status":
        # COALESCE covers the row `dial_requested()` can create before anything
        # has set a status at all: NULL is not terminal, it is "not yet known".
        return _STATUS_ASSIGNMENT
    return f"{column}=excluded.{column}"


def _insert(table: str, columns: Sequence[str], conflict: str, assignments: str | None) -> str:
    names = ", ".join(columns)
    values = ", ".join(f":{column}" for column in columns)
    tail = f"DO UPDATE SET {assignments}" if assignments else "DO NOTHING"
    return f"INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT ({conflict}) {tail}"  # noqa: S608


#: The model tables these statements write, for their column types.
_TABLES: dict[str, Any] = {
    "telephony_calls": TelephonyCall.__table__,  # type: ignore[attr-defined]
    "telephony_events": TelephonyEvent.__table__,  # type: ignore[attr-defined]
    "telephony_spans": TelephonySpan.__table__,  # type: ignore[attr-defined]
    "telephony_turns": TelephonyTurn.__table__,  # type: ignore[attr-defined]
}


class TelemetryStatements:
    """The telemetry writes. One statement per column set, built once."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, tuple[str, ...]], Executable] = {}

    def _statement(self, table: str, columns: tuple[str, ...], conflict: str, assignments: str | None) -> Executable:
        key = (table, columns)
        cached = self._cache.get(key)
        if cached is None:
            model_table = _TABLES[table]
            cached = self._cache[key] = text(_insert(table, columns, conflict, assignments)).bindparams(
                *(bindparam(column, type_=model_table.c[column].type) for column in columns)
            )
        return cached

    def events(self) -> Executable:
        """Append to the timeline. A repeated event id is ignored, not an error."""
        return self._statement("telephony_events", EVENT_COLUMNS, "event_id", None)

    def spans(self) -> Executable:
        return self._statement(
            "telephony_spans",
            SPAN_COLUMNS,
            "span_id",
            ", ".join(f"{column}=excluded.{column}" for column in _SPAN_UPDATES),
        )

    def call(self, known: dict[str, Any]) -> Executable:
        """Create or update the per-call row with whatever is known right now.

        Counters accumulate; a terminal status absorbs (see `LIVE_STATUSES`);
        everything else takes the incoming value.
        """
        columns = ("call_id", *known)
        return self._statement(
            "telephony_calls",
            columns,
            "call_id",
            ", ".join(_call_assignment(column) for column in known),
        )

    def turn(self, known: dict[str, Any]) -> Executable:
        """Write a completed turn's derived latencies.

        Upsert rather than insert: a turn cancelled by barge-in is written once
        when it is cancelled, and a late completion must not duplicate the row.
        """
        columns = ("turn_id", *known)
        return self._statement(
            "telephony_turns",
            columns,
            "turn_id",
            ", ".join(f"{column}=excluded.{column}" for column in known),
        )
