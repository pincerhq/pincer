"""Tests for cron scheduler."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from pincer.scheduler.cron import CronScheduler, Schedule


@pytest_asyncio.fixture
async def scheduler(tmp_path):
    db_path = tmp_path / "pincer.db"
    sched = CronScheduler(db_path)
    await sched.ensure_table()
    yield sched


@pytest.mark.asyncio
class TestCronScheduler:
    async def test_add_schedule(self, scheduler):
        sid = await scheduler.add(
            "test_job",
            "0 7 * * *",
            {"type": "briefing"},
            "usr_test",
            "Europe/Berlin",
        )
        assert sid is not None
        assert sid > 0

    async def test_invalid_cron(self, scheduler):
        with pytest.raises(ValueError, match="Invalid cron"):
            await scheduler.add("bad", "invalid", {"type": "test"}, "usr_test")

    async def test_invalid_timezone(self, scheduler):
        with pytest.raises(ValueError, match="Invalid timezone"):
            await scheduler.add("bad_tz", "0 7 * * *", {"type": "test"}, "usr_test", tz="Not/AZone")

    async def test_list_schedules(self, scheduler):
        await scheduler.add("a", "0 7 * * *", {"type": "test"}, "usr_test")
        await scheduler.add("b", "0 12 * * *", {"type": "test"}, "usr_test")
        result = await scheduler.list_schedules("usr_test")
        assert len(result) == 2
        names = [s["name"] for s in result]
        assert "a" in names
        assert "b" in names

    async def test_list_empty(self, scheduler):
        result = await scheduler.list_schedules("usr_nobody")
        assert result == []

    async def test_remove(self, scheduler):
        sid = await scheduler.add("x", "0 7 * * *", {"type": "test"}, "usr_test")
        assert await scheduler.remove(sid) is True
        assert await scheduler.remove(sid) is False

    async def test_toggle(self, scheduler):
        sid = await scheduler.add("y", "0 7 * * *", {"type": "test"}, "usr_test")
        assert await scheduler.toggle(sid, False) is True
        schedules = await scheduler.list_schedules("usr_test")
        assert schedules[0]["enabled"] == 0
        assert await scheduler.toggle(sid, True) is True

    async def test_different_users_isolated(self, scheduler):
        await scheduler.add("job", "0 7 * * *", {"type": "test"}, "usr_a")
        await scheduler.add("job", "0 8 * * *", {"type": "test"}, "usr_b")
        assert len(await scheduler.list_schedules("usr_a")) == 1
        assert len(await scheduler.list_schedules("usr_b")) == 1

    async def test_get_existing(self, scheduler):
        sid = await scheduler.add("z", "0 7 * * *", {"type": "briefing"}, "usr_test")
        schedule = await scheduler.get(sid)
        assert schedule is not None
        assert schedule.name == "z"
        assert schedule.action["type"] == "briefing"

    async def test_get_missing_returns_none(self, scheduler):
        assert await scheduler.get(999999) is None

    async def test_get_due_returns_past_due_only(self, scheduler):
        sid = await scheduler.add("due_now", "0 7 * * *", {"type": "test"}, "usr_test")
        await scheduler.add("far_future", "0 7 * * *", {"type": "test"}, "usr_test")

        # backdate one schedule's next_run_at so it's due, leave the other alone
        import aiosqlite

        async with aiosqlite.connect(str(scheduler._db_path)) as db:
            await db.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), sid),
            )
            await db.commit()

        due = await scheduler.get_due()
        assert [s.id for s in due] == [sid]

    async def test_get_due_skips_disabled(self, scheduler):
        sid = await scheduler.add("disabled_due", "0 7 * * *", {"type": "test"}, "usr_test")
        await scheduler.toggle(sid, False)

        import aiosqlite

        async with aiosqlite.connect(str(scheduler._db_path)) as db:
            await db.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), sid),
            )
            await db.commit()

        assert await scheduler.get_due() == []

    async def test_mark_fired_advances_next_run(self, scheduler):
        sid = await scheduler.add("advance_me", "*/5 * * * *", {"type": "test"}, "usr_test", tz="UTC")
        schedule = await scheduler.get(sid)
        due_next_run = datetime.now(UTC) - timedelta(minutes=5)
        schedule.next_run_at = due_next_run.isoformat()

        await scheduler.mark_fired(schedule)

        updated = await scheduler.get(sid)
        assert updated.last_run_at is not None
        assert datetime.fromisoformat(updated.next_run_at) > due_next_run


class TestScheduleModel:
    def test_compute_next_run(self):
        row = {
            "id": 1,
            "pincer_user_id": "u",
            "name": "t",
            "cron_expr": "0 7 * * *",
            "action": '{"type":"t"}',
            "channel": "telegram",
            "timezone": "Europe/Berlin",
            "enabled": 1,
            "last_run_at": None,
            "next_run_at": None,
        }
        s = Schedule(row)
        nxt = s.compute_next_run()
        assert nxt.tzinfo is not None
        assert nxt.hour is not None

    def test_action_parsing(self):
        row = {
            "id": 2,
            "pincer_user_id": "u",
            "name": "t",
            "cron_expr": "*/5 * * * *",
            "action": '{"type":"custom","prompt":"hello"}',
            "channel": "whatsapp",
            "timezone": "UTC",
            "enabled": 1,
        }
        s = Schedule(row)
        assert s.action["type"] == "custom"
        assert s.action["prompt"] == "hello"
        assert s.channel == "whatsapp"
