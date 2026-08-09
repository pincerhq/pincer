"""
Persistent cron-based task scheduler — storage layer.

- Standard cron expressions via croniter
- SQLite persistence (survives restarts)
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
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import aiosqlite
from croniter import croniter

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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
        self.id: int = row["id"]
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
    """SQLite-backed store for cron schedules — CRUD plus the due-schedule query."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)

    async def ensure_table(self) -> None:
        """Create schedules table if it doesn't exist."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pincer_user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    action TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'telegram',
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_next_run
                ON schedules(next_run_at) WHERE enabled = 1
            """)
            await db.commit()

    # ── CRUD ─────────────────────────────────────

    async def add(
        self,
        name: str,
        cron_expr: str,
        action: dict[str, Any],
        pincer_user_id: str,
        tz: str = "UTC",
        channel: str = "telegram",
    ) -> int:
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        try:
            tzinfo = ZoneInfo(tz)
        except Exception as e:
            raise ValueError(f"Invalid timezone: {tz}") from e

        next_run = croniter(cron_expr, datetime.now(tzinfo)).get_next(datetime)
        next_run_utc = next_run.astimezone(UTC).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """INSERT INTO schedules
                   (pincer_user_id, name, cron_expr, action, channel, timezone, next_run_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pincer_user_id, name, cron_expr, json.dumps(action), channel, tz, next_run_utc),
            )
            await db.commit()
            sid = cursor.lastrowid

        logger.info("Schedule added: %s (cron=%s, tz=%s)", name, cron_expr, tz)
        return sid  # type: ignore[return-value]

    async def remove(self, schedule_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await db.commit()
            removed = cursor.rowcount > 0
        if removed:
            logger.info("Schedule removed: id=%s", schedule_id)
        else:
            logger.warning("Schedule remove no-op: id=%s not found", schedule_id)
        return removed

    async def toggle(self, schedule_id: int, enabled: bool) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE schedules SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
                (int(enabled), schedule_id),
            )
            await db.commit()
            toggled = cursor.rowcount > 0
        if toggled:
            logger.info("Schedule %s: id=%s", "enabled" if enabled else "disabled", schedule_id)
        else:
            logger.warning("Schedule toggle no-op: id=%s not found", schedule_id)
        return toggled

    async def list_schedules(self, pincer_user_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM schedules WHERE pincer_user_id = ? ORDER BY next_run_at",
                (pincer_user_id,),
            )
            return [dict(r) for r in rows]

    async def get(self, schedule_id: int) -> Schedule | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = list(await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (schedule_id,)))
            if not rows:
                logger.debug("Schedule lookup miss: id=%s", schedule_id)
                return None
            return Schedule(dict(rows[0]))

    # ── Due-schedule query (polled by ScheduleDispatcher) ────

    async def get_due(self, now: datetime | None = None) -> list[Schedule]:
        """Enabled schedules whose next_run_at has passed. Does not mark them fired."""
        now_utc = (now or datetime.now(UTC)).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """SELECT * FROM schedules
                   WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                   ORDER BY next_run_at""",
                (now_utc,),
            )
            due = [Schedule(dict(row)) for row in rows]
        logger.debug("Due-schedule query: %d due as of %s", len(due), now_utc)
        return due

    async def mark_fired(self, schedule: Schedule) -> None:
        """Advance a schedule's next_run_at after it has been dispatched."""
        next_run = schedule.compute_next_run()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """UPDATE schedules
                   SET last_run_at = datetime('now'), next_run_at = ?,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (next_run.isoformat(), schedule.id),
            )
            await db.commit()
        logger.debug("Schedule marked fired: id=%s next_run_at=%s", schedule.id, next_run.isoformat())
