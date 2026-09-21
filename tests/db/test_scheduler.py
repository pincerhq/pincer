"""Scheduling on the repository/service layer, on SQLite and Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pincer.db.engine import get_engine
from pincer.models.scheduler import BriefingConfig, EventTrigger, Schedule
from pincer.services.scheduler import (
    DEFAULT_BRIEFING_CONFIG,
    BriefingConfigService,
    EventTriggerService,
    ScheduleService,
)

_TABLES = [Schedule.__table__, EventTrigger.__table__, BriefingConfig.__table__]


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Schedule.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: Schedule.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Schedule.metadata.drop_all(c, tables=_TABLES))


# ── schedules ────────────────────────────────────────────────────────


async def test_add_returns_the_new_id_and_computes_the_next_run(url):
    service = ScheduleService(url)
    schedule_id = await service.add("briefing", "0 8 * * *", {"type": "briefing"}, "usr_a", tz="Europe/Berlin")

    row = await service.get(schedule_id)
    assert row["name"] == "briefing"
    assert row["action"] == '{"type": "briefing"}'
    assert row["timezone"] == "Europe/Berlin"
    assert row["enabled"] == 1  # the column default
    assert datetime.fromisoformat(row["next_run_at"]) > datetime.now(UTC)


async def test_an_invalid_cron_or_timezone_is_refused(url):
    service = ScheduleService(url)
    with pytest.raises(ValueError, match="cron"):
        await service.add("bad", "not a cron", {}, "usr_a")
    with pytest.raises(ValueError, match="timezone"):
        await service.add("bad", "0 8 * * *", {}, "usr_a", tz="Mars/Olympus")
    assert await service.list_all() == []


async def test_remove_and_toggle_only_touch_the_owner_s_rows(url):
    service = ScheduleService(url)
    mine = await service.add("mine", "0 8 * * *", {}, "usr_a")

    assert await service.remove(mine, "usr_b") is False
    assert await service.toggle(mine, False, "usr_b") is False
    assert (await service.get(mine))["enabled"] == 1

    assert await service.toggle(mine, False, "usr_a") is True
    assert (await service.get(mine))["enabled"] == 0
    assert await service.remove(mine, "usr_a") is True
    assert await service.get(mine) is None


@pytest.mark.parametrize("bad_id", ["3", "nonexistent-id", ""])
async def test_an_id_that_could_never_exist_is_a_quiet_miss(url, bad_id):
    """Ids were integers before 0017, and the tool passes the model's guess through."""
    service = ScheduleService(url)
    await service.add("mine", "0 8 * * *", {}, "usr_a")

    assert await service.remove(bad_id, "usr_a") is False
    assert await service.toggle(bad_id, False, "usr_a") is False
    await service.mark_fired(bad_id, datetime.now(UTC).isoformat())
    assert len(await service.list_all()) == 1


async def test_due_returns_only_enabled_past_schedules_oldest_first(url):
    service = ScheduleService(url)
    past = await service.add("past", "0 8 * * *", {}, "usr_a")
    older = await service.add("older", "0 8 * * *", {}, "usr_a")
    disabled = await service.add("disabled", "0 8 * * *", {}, "usr_a")
    future = await service.add("future", "0 8 * * *", {}, "usr_a")

    now = datetime.now(UTC)
    await service.mark_fired(past, (now - timedelta(minutes=1)).isoformat())
    await service.mark_fired(older, (now - timedelta(hours=2)).isoformat())
    await service.mark_fired(disabled, (now - timedelta(hours=3)).isoformat())
    await service.toggle(disabled, False, "usr_a")
    await service.mark_fired(future, (now + timedelta(hours=1)).isoformat())

    assert [row["name"] for row in await service.due()] == ["older", "past"]


async def test_mark_fired_records_the_run_and_the_next_one(url):
    service = ScheduleService(url)
    schedule_id = await service.add("briefing", "0 8 * * *", {}, "usr_a")
    next_run = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    await service.mark_fired(schedule_id, next_run)

    row = await service.get(schedule_id)
    assert row["next_run_at"] == next_run
    assert row["last_run_at"] is not None
    # Same shape as SQLite's datetime('now'), which these columns already hold.
    assert datetime.strptime(row["last_run_at"], "%Y-%m-%d %H:%M:%S")


async def test_lists_are_per_user_and_across_users(url):
    service = ScheduleService(url)
    await service.add("a", "0 8 * * *", {}, "usr_a")
    await service.add("b", "0 9 * * *", {}, "usr_b")

    assert [row["name"] for row in await service.list_for_user("usr_a")] == ["a"]
    assert {row["pincer_user_id"] for row in await service.list_all()} == {"usr_a", "usr_b"}


# ── event triggers ───────────────────────────────────────────────────


async def test_an_event_is_processed_once(url):
    service = EventTriggerService(url)
    assert await service.is_processed("email", "uid-1") is False

    await service.mark_processed("email", "uid-1", "usr_a", result="replied")
    assert await service.is_processed("email", "uid-1") is True
    assert await service.is_processed("email", "uid-2") is False
    assert await service.is_processed("calendar", "uid-1") is False


async def test_marking_the_same_event_twice_is_not_an_error(url):
    """A redelivery must not raise: the pair is unique, first writer wins."""
    service = EventTriggerService(url)
    await service.mark_processed("email", "uid-1", "usr_a", result="first")
    await service.mark_processed("email", "uid-1", "usr_b", result="second")
    assert await service.is_processed("email", "uid-1") is True


# ── briefing config ──────────────────────────────────────────────────


async def test_briefing_config_is_created_on_first_read(url):
    service = BriefingConfigService(url)
    assert await service.get_or_create("usr_a") == DEFAULT_BRIEFING_CONFIG

    stored = await service.get_or_create("usr_a")
    assert stored["pincer_user_id"] == "usr_a"
    assert stored["weather_location"] == "Berlin,DE"


async def test_briefing_config_updates_only_known_fields(url):
    service = BriefingConfigService(url)
    await service.get_or_create("usr_a")

    await service.update("usr_a", weather_location="Hamburg,DE", enabled=0, nonsense=1)

    stored = await service.get_or_create("usr_a")
    assert stored["weather_location"] == "Hamburg,DE"
    assert stored["updated_at"] is not None
