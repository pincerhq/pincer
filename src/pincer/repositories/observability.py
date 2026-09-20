"""Voice observability: appointment outcomes, per-call costs, canary runs.

All three timestamp columns hold ISO-8601 UTC text, so window filters are
string comparisons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.observability import AppointmentOutcome, CallCost, CanaryRun
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class AppointmentOutcomeRepository(BaseRepository[AppointmentOutcome, int]):
    model = AppointmentOutcome

    async def record(self, values: dict[str, Any]) -> None:
        """One row per appointment task, keyed by `task_id`.

        A task can span several dial attempts and must count once, so a later
        attempt overwrites the earlier outcome rather than adding a row.
        """
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                AppointmentOutcome,
                values,
                index_elements=["task_id"],
                set_=lambda excluded: {
                    "result": excluded.result,
                    "call_sid": excluded.call_sid,
                    "attempts": excluded.attempts,
                    "detail": excluded.detail,
                    "recorded_at": excluded.recorded_at,
                },
            )
        )

    async def results_since(self, since: str) -> Sequence[str]:
        stmt = select(AppointmentOutcome.result).where(col(AppointmentOutcome.recorded_at) >= since)
        return (await self.session.exec(stmt)).all()

    async def counts_by_result(self, since: str) -> dict[str, int]:
        stmt = (
            select(AppointmentOutcome.result, func.count())
            .where(col(AppointmentOutcome.recorded_at) >= since)
            .group_by(col(AppointmentOutcome.result))
        )
        return {str(result): int(count) for result, count in (await self.session.exec(stmt)).all()}


class CallCostRepository(BaseRepository[CallCost, str]):
    model = CallCost

    async def save(self, values: dict[str, Any]) -> None:
        """One priced row per call; a re-price replaces it."""
        stored = dict(values)
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                CallCost,
                stored,
                index_elements=["call_sid"],
                set_=lambda excluded: {name: excluded[name] for name in stored if name != "call_sid"},
            )
        )

    async def recorded_since(self, since: str, end: str | None = None) -> Sequence[CallCost]:
        where = [col(CallCost.recorded_at) >= since]
        if end:
            where.append(col(CallCost.recorded_at) < end)
        return await self.list(*where)

    async def totals_for(self, call_sids: Sequence[str]) -> dict[str, float]:
        stmt = select(CallCost.call_sid, CallCost.total_usd).where(col(CallCost.call_sid).in_(list(call_sids)))
        return {sid: float(total or 0.0) for sid, total in (await self.session.exec(stmt)).all()}


class CanaryRunRepository(BaseRepository[CanaryRun, int]):
    model = CanaryRun

    async def since(self, cutoff: str, *, failed_only: bool = False) -> Sequence[CanaryRun]:
        where = [col(CanaryRun.ran_at) >= cutoff]
        if failed_only:
            where.append(col(CanaryRun.ok) == 0)
        return await self.list(*where)

    async def counts_since(self, cutoff: str) -> tuple[int, int]:
        """(runs, healthy runs) since `cutoff` — the availability SLO's input."""
        stmt = select(func.count(), func.sum(CanaryRun.ok)).where(col(CanaryRun.ran_at) >= cutoff)
        total, healthy = (await self.session.exec(stmt)).one()
        return int(total or 0), int(healthy or 0)

    async def newest(self, limit: int) -> Sequence[CanaryRun]:
        stmt = select(CanaryRun).order_by(col(CanaryRun.ran_at).desc()).limit(limit)
        return (await self.session.exec(stmt)).all()
