"""Telephony telemetry's read side: the call table, one call's timelines, and
the windows the overview and the alerts aggregate.

Built on Core `select()` over the model tables, so every parameter binds with
its column's type: an ISO string at a `TIMESTAMP` column, a uuid at a `uuid`
one, and a status or a search needle at text, whatever it happens to look like.
Values read back the same way, as the ISO strings and id strings the callers
and the dashboard expect on both dialects.

Reads run on a connection rather than an ORM session: the overview aggregates
tens of thousands of plain rows, and an identity map over them buys nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, false, func, inspect, or_, select

from pincer.db.ids import is_id
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import ColumnElement, Select
    from sqlalchemy.ext.asyncio import AsyncConnection

CALLS = TelephonyCall.__table__  # type: ignore[attr-defined]
EVENTS = TelephonyEvent.__table__  # type: ignore[attr-defined]
SPANS = TelephonySpan.__table__  # type: ignore[attr-defined]
TURNS = TelephonyTurn.__table__  # type: ignore[attr-defined]

#: Call columns a `CallFilters` field compares for equality, named alike.
_EQUALITY_FILTERS = (
    "environment",
    "app_version",
    "tenant_id",
    "direction",
    "provider",
    "engine",
    "model",
    "language",
    "status",
    "failure_category",
    "failure_code",
)

#: Call columns the search box matches, case-insensitively, anywhere in the value.
_SEARCHED = ("call_id", "provider_call_id", "trace_id", "from_number_masked", "to_number_masked")

#: Call columns the call table may be sorted by.
SORTABLE = frozenset(
    {"registered_at", "duration_ms", "turn_count", "setup_ms", "status", "direction", "engine", "failure_code"}
)


@dataclass(slots=True)
class CallFilters:
    """Everything the overview and the call table can be sliced by."""

    since: str = ""
    until: str = ""
    environment: str = ""
    app_version: str = ""
    tenant_id: str = ""
    direction: str = ""
    provider: str = ""
    engine: str = ""
    model: str = ""
    language: str = ""
    status: str = ""
    failure_category: str = ""
    failure_code: str = ""
    search: str = ""

    @classmethod
    def for_hours(cls, hours: float, **kwargs: Any) -> CallFilters:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        return cls(since=since, **kwargs)

    def where(self) -> list[ColumnElement[bool]]:
        """The filters as conditions on `telephony_calls`; none means every call."""
        conditions: list[ColumnElement[bool]] = [
            CALLS.c[column] == value for column in _EQUALITY_FILTERS if (value := getattr(self, column))
        ]
        if self.since:
            conditions.append(CALLS.c.registered_at >= self.since)
        if self.until:
            conditions.append(CALLS.c.registered_at <= self.until)
        needle = self.search.strip()
        if needle:
            pattern = f"%{_escape_like(needle)}%"
            # `call_id` is a uuid on Postgres, which has no `uuid LIKE text`;
            # the cast costs no index, since a leading wildcard could use none.
            conditions.append(or_(*(cast(CALLS.c[column], String).ilike(pattern, escape="\\") for column in _SEARCHED)))
        return conditions


@dataclass(slots=True)
class TenantScope:
    """Which tenants the caller may see.

    ``None`` means unrestricted (single-tenant deployment or an operator with
    global access). An empty tuple means "no tenants" and every query returns
    nothing — fail closed, never fail open.
    """

    allowed: tuple[str, ...] | None = None

    def where(self) -> list[ColumnElement[bool]]:
        if self.allowed is None:
            return []
        if not self.allowed:
            return [false()]
        return [CALLS.c.tenant_id.in_(self.allowed)]


def _escape_like(value: str) -> str:
    """A search term taken literally: `%` and `_` in it are characters, not wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _joined(*columns: Any) -> Select[Any]:
    """Turns, each with the named columns of its call."""
    return select(TURNS, *columns).select_from(TURNS.join(CALLS, CALLS.c.call_id == TURNS.c.call_id))


class TelemetryReads:
    """The telemetry reads, on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def tables_present(self) -> bool:
        """True once the telemetry tables exist — a fresh database has none."""
        wanted = {CALLS.name, EVENTS.name, SPANS.name, TURNS.name}
        names = await self._conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        return wanted <= names

    async def _rows(self, stmt: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in (await self._conn.execute(stmt)).mappings().all()]

    async def count_calls(self, where: Sequence[ColumnElement[bool]]) -> int:
        return int((await self._conn.execute(select(func.count()).select_from(CALLS).where(*where))).scalar_one())

    async def calls(
        self,
        where: Sequence[ColumnElement[bool]],
        *,
        limit: int,
        offset: int = 0,
        sort: str = "registered_at",
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        column = CALLS.c[sort if sort in SORTABLE else "registered_at"]
        stmt = (
            select(CALLS)
            .where(*where)
            .order_by(column.desc() if descending else column.asc())
            .limit(limit)
            .offset(offset)
        )
        return await self._rows(stmt)

    async def call(self, ref: str, where: Sequence[ColumnElement[bool]] = ()) -> dict[str, Any] | None:
        """One call by internal id or by provider CallSid.

        A reference that is not a uuid can only be a CallSid, so the uuid
        column is not compared at all — it would refuse the value on bind.
        """
        match = CALLS.c.provider_call_id == ref
        if is_id(ref):
            match = or_(CALLS.c.call_id == ref, match)
        rows = await self._rows(select(CALLS).where(match, *where).limit(1))
        return rows[0] if rows else None

    async def events(self, call_id: str, *, limit: int) -> list[dict[str, Any]]:
        """A call's timeline, ordered at read time: arrival order is not trusted."""
        if not is_id(call_id):
            return []
        stmt = (
            select(EVENTS)
            .where(EVENTS.c.call_id == call_id)
            .order_by(EVENTS.c.ts_utc.asc(), EVENTS.c.seq.asc())
            .limit(limit)
        )
        return await self._rows(stmt)

    async def spans(self, call_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not is_id(call_id):
            return []
        stmt = select(SPANS).where(SPANS.c.call_id == call_id).order_by(SPANS.c.start_mono_ns.asc()).limit(limit)
        return await self._rows(stmt)

    async def turns(self, call_id: str) -> list[dict[str, Any]]:
        if not is_id(call_id):
            return []
        return await self._rows(select(TURNS).where(TURNS.c.call_id == call_id).order_by(TURNS.c.turn_no.asc()))

    async def slowest_turns(self, where: Sequence[ColumnElement[bool]], *, limit: int) -> list[dict[str, Any]]:
        stmt = (
            _joined(CALLS.c.provider_call_id, CALLS.c.direction)
            .where(*where, TURNS.c.response_latency_ms.isnot(None))
            .order_by(TURNS.c.response_latency_ms.desc())
            .limit(limit)
        )
        return await self._rows(stmt)

    async def newest_turns(self, where: Sequence[ColumnElement[bool]], *, limit: int) -> list[dict[str, Any]]:
        """Turns in the window, newest first, each with its call's provider id."""
        stmt = _joined(CALLS.c.provider_call_id).where(*where).order_by(TURNS.c.created_at.desc()).limit(limit)
        return await self._rows(stmt)

    async def turns_with_call_facts(self, where: Sequence[ColumnElement[bool]], *, limit: int) -> list[dict[str, Any]]:
        """Turns with what the overview compares them by, named apart from the turn's own columns."""
        stmt = (
            _joined(
                CALLS.c.engine.label("call_engine"),
                CALLS.c.model.label("call_model"),
                CALLS.c.provider.label("call_provider"),
                CALLS.c.direction.label("call_direction"),
                CALLS.c.registered_at.label("call_registered_at"),
            )
            .where(*where)
            .limit(limit)
        )
        return await self._rows(stmt)

    async def event_counts(self, where: Sequence[ColumnElement[bool]], names: Sequence[str]) -> dict[str, int]:
        """How often each named event occurred on the calls `where` selects."""
        stmt = (
            select(EVENTS.c.name, func.count().label("n"))
            .select_from(EVENTS.join(CALLS, CALLS.c.call_id == EVENTS.c.call_id))
            .where(*where, EVENTS.c.name.in_(list(names)))
            .group_by(EVENTS.c.name)
        )
        return {str(row["name"]): int(row["n"]) for row in await self._rows(stmt)}

    async def turns_since(self, since: str) -> list[dict[str, Any]]:
        """Every turn created since `since`, with its call's provider id."""
        return await self._rows(_joined(CALLS.c.provider_call_id).where(TURNS.c.created_at >= since))

    async def events_since(self, since: str, names: Sequence[str]) -> list[dict[str, Any]]:
        """The named events since `since`, with their call's provider id."""
        stmt = (
            select(EVENTS.c.attributes, EVENTS.c.name, CALLS.c.provider_call_id)
            .select_from(EVENTS.join(CALLS, CALLS.c.call_id == EVENTS.c.call_id))
            .where(EVENTS.c.ts_utc >= since, EVENTS.c.name.in_(list(names)))
        )
        return await self._rows(stmt)
