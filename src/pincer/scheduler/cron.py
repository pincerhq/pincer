"""
Persistent cron-based task scheduler.

- Standard cron expressions via croniter
- SQLite persistence (survives restarts)
- Timezone-aware (per-schedule timezone)
- Async loop checking every 60 seconds
- Action handlers registered by type (briefing, custom, etc.)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import aiosqlite
from croniter import croniter

from pincer.channels.base import ChannelType

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
        self.action: dict[str, Any] = (
            json.loads(row["action"]) if isinstance(row["action"], str) else row["action"]
        )
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
        return croniter(self.cron_expr, base).get_next(datetime).astimezone(timezone.utc)


class CronScheduler:
    """Async cron scheduler backed by SQLite."""

    def __init__(self, db_path: Path, router: Any) -> None:
        self._db_path = str(db_path)
        self._router = router
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._action_handlers: dict[str, Callable[..., Coroutine[Any, Any, str | None]]] = {}
        self._check_interval = 60

    def register_action(
        self,
        action_type: str,
        handler: Callable[..., Coroutine[Any, Any, str | None]],
    ) -> None:
        self._action_handlers[action_type] = handler
        logger.debug("Scheduler action registered: %s", action_type)

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

        next_run = croniter(cron_expr, datetime.now(ZoneInfo(tz))).get_next(datetime)
        next_run_utc = next_run.astimezone(timezone.utc).isoformat()

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
            return cursor.rowcount > 0

    async def toggle(self, schedule_id: int, enabled: bool) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE schedules SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
                (int(enabled), schedule_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_schedules(self, pincer_user_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM schedules WHERE pincer_user_id = ? ORDER BY next_run_at",
                (pincer_user_id,),
            )
            return [dict(r) for r in rows]

    # ── Loop ─────────────────────────────────────

    async def start(self) -> None:
        await self.ensure_table()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="pincer-scheduler")
        logger.info("Scheduler started (interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_and_fire()
            except Exception:
                logger.exception("Scheduler loop error")
            await asyncio.sleep(self._check_interval)

    async def _check_and_fire(self) -> None:
        now_utc = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            due = await db.execute_fetchall(
                """SELECT * FROM schedules
                   WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                   ORDER BY next_run_at""",
                (now_utc,),
            )

            for row in due:
                schedule = Schedule(dict(row))
                logger.info("Scheduler firing: %s (user=%s)", schedule.name, schedule.pincer_user_id)

                asyncio.create_task(
                    self._execute_action(schedule),
                    name=f"schedule-{schedule.id}",
                )

                next_run = schedule.compute_next_run()
                await db.execute(
                    """UPDATE schedules
                       SET last_run_at = datetime('now'), next_run_at = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (next_run.isoformat(), schedule.id),
                )

            if due:
                await db.commit()

    async def _execute_action(self, schedule: Schedule) -> None:
        try:
            action_type = schedule.action.get("type", "custom")
            handler = self._action_handlers.get(action_type)
            if not handler:
                logger.warning("No handler for action type: %s", action_type)
                return

            result = await handler(
                pincer_user_id=schedule.pincer_user_id,
                action=schedule.action,
                channel=schedule.channel,
            )

            if result and isinstance(result, str):
                channel_type = ChannelType(schedule.channel)
                await self._router.send_to_user(
                    schedule.pincer_user_id, result, prefer=channel_type,
                )
        except Exception:
            logger.exception("Schedule action failed: %s", schedule.name)
