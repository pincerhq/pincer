"""Voice observability storage: bookings, per-call costs, canary runs.

Three small services rather than one: they share a database, nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.session import session_scope
from pincer.models.observability import AppointmentOutcome, CallCost, CanaryRun
from pincer.repositories.observability import (
    AppointmentOutcomeRepository,
    CallCostRepository,
    CanaryRunRepository,
)
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class BookingsService(DatabaseService):
    async def record_outcome(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await AppointmentOutcomeRepository(session).record(values)

    async def results_since(self, since: str) -> list[str]:
        async with session_scope(self._url) as session:
            return [str(result) for result in await AppointmentOutcomeRepository(session).results_since(since)]

    async def counts_by_result(self, since: str) -> dict[str, int]:
        async with session_scope(self._url) as session:
            return await AppointmentOutcomeRepository(session).counts_by_result(since)


class CallCostsService(DatabaseService):
    async def save(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await CallCostRepository(session).save(values)

    async def get(self, call_sid: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await CallCostRepository(session).get(call_sid)
        return None if row is None else {name: getattr(row, name) for name in CallCost.model_fields}

    async def recorded_since(self, since: str, end: str | None = None) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await CallCostRepository(session).recorded_since(since, end)
        return [{name: getattr(row, name) for name in CallCost.model_fields} for row in rows]

    async def totals_for(self, call_sids: Sequence[str]) -> dict[str, float]:
        if not call_sids:
            return {}
        async with session_scope(self._url) as session:
            return await CallCostRepository(session).totals_for(call_sids)


class CanaryService(DatabaseService):
    async def record_run(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await CanaryRunRepository(session).add(CanaryRun(**values), refresh=False)

    async def runs_since(self, cutoff: str, *, failed_only: bool = False) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await CanaryRunRepository(session).since(cutoff, failed_only=failed_only)
        return [{name: getattr(row, name) for name in CanaryRun.model_fields} for row in rows]

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs, newest first."""
        async with session_scope(self._url) as session:
            rows = await CanaryRunRepository(session).newest(limit)
        return [
            {
                "ran_at": row.ran_at,
                "ok": row.ok,
                "skipped": row.skipped,
                "reason": row.reason,
                "call_sid": row.call_sid,
                "turns": row.turns,
                "duration_s": row.duration_s,
            }
            for row in rows
        ]


__all__ = ["AppointmentOutcome", "BookingsService", "CallCostsService", "CanaryService"]
