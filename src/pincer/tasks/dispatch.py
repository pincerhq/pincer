"""Cron schedule polling — the "when" repid deliberately doesn't provide.

Keeps the same SQLite poll loop shape the old CronScheduler used internally,
but instead of firing an in-process asyncio.create_task it enqueues a repid
message so execution is durable, ack'd, and retryable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from pincer.tasks.app import TASK_CHANNEL

if TYPE_CHECKING:
    from repid import Repid

    from pincer.scheduler.cron import CronScheduler

logger = logging.getLogger(__name__)


class ScheduleDispatcher:
    """Polls a `CronScheduler` store for due schedules and enqueues them via repid."""

    def __init__(self, store: CronScheduler, app: Repid, *, interval: int = 60) -> None:
        self._store = store
        self._app = app
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="pincer-schedule-dispatcher")
        logger.info("Schedule dispatcher started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Schedule dispatcher stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_and_dispatch()
            except Exception:
                logger.exception("Schedule dispatcher loop error")
            await asyncio.sleep(self._interval)

    async def _check_and_dispatch(self) -> None:
        due = await self._store.get_due()
        logger.debug("Schedule dispatcher tick: %d due", len(due))
        for schedule in due:
            try:
                logger.info(
                    "Dispatching schedule: id=%s name=%s user=%s",
                    schedule.id,
                    schedule.name,
                    schedule.pincer_user_id,
                )
                await self._app.send_message_json(
                    channel=TASK_CHANNEL,
                    payload={"schedule_id": schedule.id},
                    headers={"topic": "run_scheduled_action"},
                )
                await self._store.mark_fired(schedule)
                logger.debug("Schedule dispatched and marked fired: id=%s", schedule.id)
            except Exception:
                logger.exception(
                    "Failed to dispatch schedule id=%s name=%s — will retry next tick",
                    schedule.id,
                    schedule.name,
                )
