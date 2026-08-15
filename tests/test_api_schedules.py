"""Tests for the /api/schedules endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.api.schedules import router
from pincer.scheduler.cron import CronScheduler


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest_asyncio.fixture
async def scheduler(tmp_path: Path) -> AsyncIterator[CronScheduler]:
    db_path = tmp_path / "pincer.db"
    sched = CronScheduler(db_path)
    await sched.ensure_table()
    yield sched


def _fake_settings(db_path: str) -> Any:
    settings = type("Settings", (), {})()
    settings.db_path = db_path
    return settings


async def _mark_fired(scheduler: CronScheduler, schedule_id: int) -> None:
    """Simulate a one-off schedule having already fired (sets last_run_at)."""
    async with aiosqlite.connect(scheduler._db_path) as db:
        await db.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), schedule_id),
        )
        await db.commit()


@pytest.mark.asyncio
class TestSchedulesApi:
    async def test_empty_db(self, client: TestClient, scheduler: CronScheduler) -> None:
        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules")
        assert resp.status_code == 200
        assert resp.json() == {"tasks": [], "total": 0, "future_count": 0, "past_count": 0}

    async def test_default_includes_recurring_and_upcoming_one_off(
        self, client: TestClient, scheduler: CronScheduler
    ) -> None:
        await scheduler.add("daily digest", "0 8 * * *", {"type": "briefing"}, "usr_a")
        await scheduler.add("one_off", "5 9 15 8 *", {"type": "custom", "prompt": "hi"}, "usr_a")

        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules")

        data = resp.json()
        assert data["total"] == 2
        assert data["future_count"] == 2
        assert data["past_count"] == 0
        names = {t["name"] for t in data["tasks"]}
        assert names == {"daily digest", "one_off"}
        kinds = {t["name"]: t["kind"] for t in data["tasks"]}
        assert kinds["daily digest"] == "recurring"
        assert kinds["one_off"] == "one_time"
        assert all(t["timing"] == "future" for t in data["tasks"])

    async def test_future_tasks_ordered_ascending_by_next_run(
        self, client: TestClient, scheduler: CronScheduler
    ) -> None:
        sid_later = await scheduler.add("later", "0 8 * * *", {"type": "briefing"}, "usr_a")
        sid_sooner = await scheduler.add("sooner", "0 8 * * *", {"type": "briefing"}, "usr_a")

        async with aiosqlite.connect(scheduler._db_path) as db:
            now = datetime.now(UTC)
            await db.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                ((now + timedelta(days=5)).isoformat(), sid_later),
            )
            await db.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                ((now + timedelta(hours=1)).isoformat(), sid_sooner),
            )
            await db.commit()

        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules")

        data = resp.json()
        assert [t["name"] for t in data["tasks"]] == ["sooner", "later"]

    async def test_fired_one_off_excluded_by_default(self, client: TestClient, scheduler: CronScheduler) -> None:
        sid = await scheduler.add("one_off", "5 9 15 8 *", {"type": "custom", "prompt": "hi"}, "usr_a")
        await _mark_fired(scheduler, sid)

        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules")

        data = resp.json()
        assert data["total"] == 0
        assert data["future_count"] == 0
        assert data["past_count"] == 1

    async def test_include_past_returns_fired_one_offs_descending_by_last_run(
        self, client: TestClient, scheduler: CronScheduler
    ) -> None:
        sid_older = await scheduler.add("older", "5 9 15 8 *", {"type": "custom", "prompt": "a"}, "usr_a")
        sid_newer = await scheduler.add("newer", "5 9 16 8 *", {"type": "custom", "prompt": "b"}, "usr_a")

        async with aiosqlite.connect(scheduler._db_path) as db:
            now = datetime.now(UTC)
            await db.execute(
                "UPDATE schedules SET last_run_at = ? WHERE id = ?",
                ((now - timedelta(days=5)).isoformat(), sid_older),
            )
            await db.execute(
                "UPDATE schedules SET last_run_at = ? WHERE id = ?",
                ((now - timedelta(hours=1)).isoformat(), sid_newer),
            )
            await db.commit()

        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules", params={"include_past": "true"})

        data = resp.json()
        past_names = [t["name"] for t in data["tasks"] if t["timing"] == "past"]
        assert past_names == ["newer", "older"]
        assert data["past_count"] == 2

    async def test_lists_across_all_users(self, client: TestClient, scheduler: CronScheduler) -> None:
        await scheduler.add("job_a", "0 8 * * *", {"type": "briefing"}, "usr_a")
        await scheduler.add("job_b", "0 9 * * *", {"type": "briefing"}, "usr_b")

        with patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)):
            resp = client.get("/api/schedules")

        data = resp.json()
        assert {t["pincer_user_id"] for t in data["tasks"]} == {"usr_a", "usr_b"}

    async def test_db_failure_returns_500_not_empty_payload(self, client: TestClient, scheduler: CronScheduler) -> None:
        """Regression: a DB/migration failure must surface as an error, not look like 'no schedules'."""
        with (
            patch("pincer.api.schedules.get_settings_relaxed", return_value=_fake_settings(scheduler._db_path)),
            patch(
                "pincer.api.schedules.CronScheduler.list_all",
                side_effect=RuntimeError("db exploded"),
            ),
        ):
            resp = client.get("/api/schedules")

        assert resp.status_code == 500
        assert resp.json() != {"tasks": [], "total": 0, "future_count": 0, "past_count": 0}
