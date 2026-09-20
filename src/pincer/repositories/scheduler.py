"""Cron schedules, processed event triggers and briefing settings.

`schedules.next_run_at` / `last_run_at` hold ISO-8601 UTC text on both
dialects, so ordering and "has it passed" are plain string comparisons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.scheduler import BriefingConfig, EventTrigger, Schedule
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class ScheduleRepository(BaseRepository[Schedule, str]):
    model = Schedule

    async def for_user(self, pincer_user_id: str) -> Sequence[Schedule]:
        return await self.list(col(Schedule.pincer_user_id) == pincer_user_id, order_by=[col(Schedule.next_run_at)])

    async def due(self, now_utc: str) -> Sequence[Schedule]:
        """Enabled schedules whose `next_run_at` has passed. Does not mark them fired."""
        return await self.list(
            col(Schedule.enabled) == 1,
            col(Schedule.next_run_at).isnot(None),
            col(Schedule.next_run_at) <= now_utc,
            order_by=[col(Schedule.next_run_at)],
        )

    async def delete_for_user(self, schedule_id: str, pincer_user_id: str) -> int:
        return await self.delete_where(col(Schedule.id) == schedule_id, col(Schedule.pincer_user_id) == pincer_user_id)

    async def set_enabled(self, schedule_id: str, enabled: bool, pincer_user_id: str, *, now: str) -> int:
        """Returns the number of rows changed — 0 when the id is another user's."""
        return await self._update(
            {"enabled": int(enabled), "updated_at": now},
            col(Schedule.id) == schedule_id,
            col(Schedule.pincer_user_id) == pincer_user_id,
        )

    async def mark_fired(self, schedule_id: str, *, next_run_at: str, now: str) -> int:
        return await self._update(
            {"last_run_at": now, "next_run_at": next_run_at, "updated_at": now},
            col(Schedule.id) == schedule_id,
        )

    async def _update(self, values: dict[str, Any], *where: Any) -> int:
        from sqlalchemy import update

        result = await self.session.exec(update(Schedule).where(*where).values(**values))
        return int(result.rowcount)


class EventTriggerRepository(BaseRepository[EventTrigger, str]):
    model = EventTrigger

    async def is_processed(self, trigger_type: str, trigger_key: str) -> bool:
        stmt = select(EventTrigger.id).where(
            col(EventTrigger.trigger_type) == trigger_type, col(EventTrigger.trigger_key) == trigger_key
        )
        return (await self.session.exec(stmt)).first() is not None

    async def mark_processed(self, trigger_type: str, trigger_key: str, pincer_user_id: str, result: str = "") -> None:
        """First writer wins: `(trigger_type, trigger_key)` is unique, and a
        second delivery of the same event must not raise."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                EventTrigger,
                {
                    "trigger_type": trigger_type,
                    "trigger_key": trigger_key,
                    "pincer_user_id": pincer_user_id,
                    "result": result,
                },
                index_elements=["trigger_type", "trigger_key"],
            )
        )


class BriefingConfigRepository(BaseRepository[BriefingConfig, str]):
    model = BriefingConfig

    async def for_user(self, pincer_user_id: str) -> BriefingConfig | None:
        stmt = select(BriefingConfig).where(col(BriefingConfig.pincer_user_id) == pincer_user_id)
        return (await self.session.exec(stmt)).first()

    async def update_for_user(self, pincer_user_id: str, values: dict[str, Any], *, now: str) -> int:
        from sqlalchemy import update

        result = await self.session.exec(
            update(BriefingConfig)
            .where(col(BriefingConfig.pincer_user_id) == pincer_user_id)
            .values(**values, updated_at=now)
        )
        return int(result.rowcount)
