"""Tests for pincer.tasks.dispatch.ScheduleDispatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from pincer.scheduler.cron import CronScheduler
from pincer.tasks.dispatch import ScheduleDispatcher


@pytest_asyncio.fixture
async def store(tmp_path):
    sched = CronScheduler(tmp_path / "pincer.db")
    await sched.ensure_table()
    return sched


async def _backdate(store: CronScheduler, schedule_id: int) -> None:
    async with aiosqlite.connect(str(store._db_path)) as db:
        await db.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), schedule_id),
        )
        await db.commit()


@pytest.mark.asyncio
class TestScheduleDispatcher:
    async def test_dispatches_due_schedule_and_marks_fired(self, store):
        sid = await store.add("job", "0 7 * * *", {"type": "briefing"}, "usr_test")
        await _backdate(store, sid)

        fake_app = AsyncMock()
        dispatcher = ScheduleDispatcher(store, fake_app, interval=60)

        await dispatcher._check_and_dispatch()

        fake_app.send_message_json.assert_awaited_once()
        _, kwargs = fake_app.send_message_json.call_args
        assert kwargs["payload"] == {"schedule_id": sid}
        assert kwargs["headers"] == {"topic": "run_scheduled_action"}

        updated = await store.get(sid)
        assert updated.last_run_at is not None

    async def test_no_due_schedules_does_not_dispatch(self, store):
        await store.add("future", "0 7 * * *", {"type": "test"}, "usr_test")
        fake_app = AsyncMock()
        dispatcher = ScheduleDispatcher(store, fake_app)

        await dispatcher._check_and_dispatch()

        fake_app.send_message_json.assert_not_awaited()

    async def test_start_stop(self, store):
        fake_app = AsyncMock()
        dispatcher = ScheduleDispatcher(store, fake_app, interval=60)
        await dispatcher.start()
        assert dispatcher._running is True
        await dispatcher.stop()
        assert dispatcher._running is False
