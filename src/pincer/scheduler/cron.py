"""
Persistent cron-based task scheduler — storage layer.

- Standard cron expressions via croniter
- Persistence via `pincer.services.scheduler` (survives restarts)
- Timezone-aware (per-schedule timezone)

`CronScheduler` only owns CRUD and the due-schedule query; deciding *when*
to poll and dispatching due schedules for durable execution lives in
`pincer.tasks.dispatch.ScheduleDispatcher` (repid has no native scheduler,
so that poll loop is still hand-rolled — this class is the SQLite-backed
source of truth it reads from and updates).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from pincer.db.engine import get_database_url
from pincer.services.scheduler import ScheduleService

logger = logging.getLogger(__name__)


def is_one_time_cron(cron_expr: str) -> bool:
    """True if `cron_expr` is a fixed-date one-off rather than a recurring schedule.

    `schedule_tool.py`'s `run_in_minutes` path pins minute/hour/day/month and
    leaves weekday as `*` (`"{minute} {hour} {day} {month} *"`) — that's the
    only way this codebase produces a schedule that fires exactly once. A
    cron_expr with both day-of-month and month pinned matches that shape;
    anything with either field wildcarded (daily/monthly/weekday-based, etc.)
    recurs indefinitely.
    """
    fields = cron_expr.split()
    if len(fields) != 5:
        return False
    day_of_month, month = fields[2], fields[3]
    return day_of_month != "*" and month != "*"


class Schedule:
    """A single scheduled task loaded from SQLite."""

    __slots__ = (
        "id",
        "pincer_user_id",
        "name",
        "cron_expr",
        "action",
        "channel",
        "tz",
        "enabled",
        "last_run_at",
        "next_run_at",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.id: str = row["id"]
        self.pincer_user_id: str = row["pincer_user_id"]
        self.name: str = row["name"]
        self.cron_expr: str = row["cron_expr"]
        self.action: dict[str, Any] = json.loads(row["action"]) if isinstance(row["action"], str) else row["action"]
        self.channel: str = row["channel"]
        self.tz: str = row["timezone"]
        self.enabled: bool = bool(row["enabled"])
        self.last_run_at: str | None = row.get("last_run_at")
        self.next_run_at: str | None = row.get("next_run_at")

    def compute_next_run(self, from_time: datetime | None = None) -> datetime:
        """Calculate next run time. Returns UTC datetime."""
        tzinfo = ZoneInfo(self.tz)
        base = from_time or datetime.now(tzinfo)
        if base.tzinfo is None:
            base = base.replace(tzinfo=tzinfo)
        return croniter(self.cron_expr, base).get_next(datetime).astimezone(UTC)


class CronScheduler:
    """Store for cron schedules — CRUD plus the due-schedule query.

    Compatibility façade over `pincer.services.scheduler.ScheduleService`; new
    code uses the service directly.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        self._service: ScheduleService | None = None

    async def ensure_table(self) -> None:
        """Ensure the schedules table is at head (see pincer.db.migrations)."""
        self._service = await ScheduleService.for_path(Path(self._db_path))

    @property
    def _svc(self) -> ScheduleService:
        """The service, built on first use for callers that skip `ensure_table`."""
        if self._service is None:
            self._service = ScheduleService(get_database_url(Path(self._db_path)))
        return self._service

    # ── CRUD ─────────────────────────────────────

    async def add(
        self,
        name: str,
        cron_expr: str,
        action: dict[str, Any],
        pincer_user_id: str,
        tz: str = "UTC",
        channel: str = "telegram",
    ) -> str:
        return await self._svc.add(name, cron_expr, action, pincer_user_id, tz, channel)

    async def remove(self, schedule_id: str, pincer_user_id: str) -> bool:
        return await self._svc.remove(schedule_id, pincer_user_id)

    async def toggle(self, schedule_id: str, enabled: bool, pincer_user_id: str) -> bool:
        return await self._svc.toggle(schedule_id, enabled, pincer_user_id)

    async def list_schedules(self, pincer_user_id: str) -> list[dict[str, Any]]:
        return await self._svc.list_for_user(pincer_user_id)

    async def list_all(self) -> list[dict[str, Any]]:
        """All schedules across all users, unordered (caller classifies/sorts)."""
        return await self._svc.list_all()

    async def get(self, schedule_id: str) -> Schedule | None:
        row = await self._svc.get(schedule_id)
        return Schedule(row) if row is not None else None

    # ── Due-schedule query (polled by ScheduleDispatcher) ────

    async def get_due(self, now: datetime | None = None) -> list[Schedule]:
        """Enabled schedules whose next_run_at has passed. Does not mark them fired."""
        return [Schedule(row) for row in await self._svc.due(now)]

    async def mark_fired(self, schedule: Schedule) -> None:
        """Advance a schedule's next_run_at after it has been dispatched."""
        await self._svc.mark_fired(schedule.id, schedule.compute_next_run().isoformat())
