"""Tests for the schedule_create/list/remove/toggle builtin tools."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio

from pincer.scheduler.cron import CronScheduler
from pincer.tools.builtin.schedule_tool import (
    make_schedule_create_handler,
    schedule_list,
    schedule_remove,
    schedule_toggle,
)
from pincer.tools.registry import ToolRegistry

CTX = {"pincer_user_id": "usr_test", "channel_name": "telegram"}


@pytest_asyncio.fixture(autouse=True)
async def _ensure_table(settings):
    settings.ensure_dirs()
    await CronScheduler(settings.db_path).ensure_table()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def websearch(query: str) -> str:
        return "ok"

    reg.register(name="websearch__search", description="search", handler=websearch)
    return reg


@pytest.fixture
def schedule_create(registry):
    return make_schedule_create_handler(registry)


@pytest.mark.asyncio
class TestScheduleCreate:
    async def test_create_happy_path(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="ukraine_digest",
                cron_expr="0 8 * * *",
                prompt="Search the web for the latest Ukraine war news and summarize it.",
                tz="Europe/Berlin",
                context=CTX,
            )
        assert "Scheduled 'ukraine_digest'" in result
        assert "telegram" in result

    async def test_synthetic_fallback_identity_rejected(self, settings, schedule_create):
        """'{channel}:{native_id}' is IdentityMiddleware's unresolved-identity fallback —
        never persisted, so a schedule stored under it could never be delivered."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="x",
                cron_expr="0 8 * * *",
                prompt="p",
                context={"pincer_user_id": "telegram:559020", "channel_name": "telegram"},
            )
        assert result.startswith("Error")
        assert "identity isn't fully linked" in result

    async def test_no_identity_returns_error(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="x", cron_expr="0 8 * * *", prompt="p", context={"channel_name": "telegram"}
            )
        assert result.startswith("Error")

    async def test_invalid_channel(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="x", cron_expr="0 8 * * *", prompt="p", channel="not_a_channel", context=CTX
            )
        assert "invalid channel" in result.lower()

    async def test_invalid_cron_surfaces_as_error_string(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="x", cron_expr="not a cron", prompt="p", context=CTX)
        assert result.startswith("Error")

    async def test_invalid_timezone_surfaces_as_error_string(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="x", cron_expr="0 8 * * *", prompt="p", tz="Not/AZone", context=CTX)
        assert result.startswith("Error")

    async def test_duplicate_name_rejected(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="dup", cron_expr="0 8 * * *", prompt="p", context=CTX)
            result = await schedule_create(name="dup", cron_expr="0 9 * * *", prompt="p2", context=CTX)
        assert "already exists" in result

    async def test_cap_enforced(self, settings, schedule_create):
        settings.max_schedules_per_user = 2
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            await schedule_create(name="b", cron_expr="0 8 * * *", prompt="p", context=CTX)
            result = await schedule_create(name="c", cron_expr="0 8 * * *", prompt="p", context=CTX)
        assert "limit" in result.lower()

    async def test_allowed_tools_valid(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="restricted",
                cron_expr="0 8 * * *",
                prompt="p",
                allowed_tools=["websearch__search"],
                context=CTX,
            )
        assert "Scheduled 'restricted'" in result

    async def test_allowed_tools_unknown_rejected(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(
                name="restricted", cron_expr="0 8 * * *", prompt="p", allowed_tools=["nonexistent_tool"], context=CTX
            )
        assert "unknown tool" in result.lower()

    async def test_requires_exactly_one_of_cron_expr_or_run_in_minutes(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            neither = await schedule_create(name="x", prompt="p", context=CTX)
            both = await schedule_create(name="y", prompt="p", cron_expr="0 8 * * *", run_in_minutes=10, context=CTX)
        assert "exactly one" in neither.lower()
        assert "exactly one" in both.lower()

    async def test_run_in_minutes_creates_one_time_schedule(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="reminder", prompt="p", run_in_minutes=10, context=CTX)
        assert "runs once" in result.lower()

        from pincer.scheduler.cron import CronScheduler

        store = CronScheduler(settings.db_path)
        rows = await store.list_schedules("usr_test")
        assert len(rows) == 1
        # a one-time cron is pinned to a specific day+month, so it can't be "every minute"
        assert rows[0]["cron_expr"] != "* * * * *"

    async def test_run_in_minutes_zero_rejected_as_neither_arg_passed(self, settings, schedule_create):
        """run_in_minutes=0 is falsy, so it trips the 'exactly one of' check, not the >=1 check."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="x", prompt="p", run_in_minutes=0, context=CTX)
        assert "exactly one" in result.lower()

    async def test_run_in_minutes_rejects_negative(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="x", prompt="p", run_in_minutes=-5, context=CTX)
        assert result.startswith("Error")
        assert "at least 1" in result

    async def test_scheduler_add_value_error_surfaces_as_error_string(self, settings, schedule_create):
        """A ValueError raised by the storage layer (e.g. a race on validation) becomes an Error string."""
        with (
            patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings),
            patch(
                "pincer.tools.builtin.schedule_tool.CronScheduler.add",
                side_effect=ValueError("boom"),
            ),
        ):
            result = await schedule_create(name="x", cron_expr="0 8 * * *", prompt="p", context=CTX)
        assert result == "Error: boom"

    async def test_too_frequent_cron_expr_rejected(self, settings, schedule_create):
        """The exact incident this guards against: an LLM-hallucinated '* * * * *'."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_create(name="spammy", prompt="p", cron_expr="* * * * *", context=CTX)
        assert "more often than every" in result.lower()

    async def test_default_timezone_falls_back_to_app_setting(self, settings, schedule_create):
        settings.timezone = "Asia/Tokyo"
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="x", prompt="p", cron_expr="0 8 * * *", context=CTX)

        from pincer.scheduler.cron import CronScheduler

        store = CronScheduler(settings.db_path)
        rows = await store.list_schedules("usr_test")
        assert rows[0]["timezone"] == "Asia/Tokyo"


@pytest.mark.asyncio
class TestScheduleListRemoveToggle:
    async def test_list_empty(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_list(context=CTX)
        assert "no scheduled jobs" in result.lower()

    async def test_list_no_identity_returns_error(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_list(context={"channel_name": "telegram"})
        assert result.startswith("Error")

    async def test_remove_no_identity_returns_error(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_remove(name="a", context={"channel_name": "telegram"})
        assert result.startswith("Error")

    async def test_toggle_no_identity_returns_error(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_toggle(name="a", enabled=False, context={"channel_name": "telegram"})
        assert result.startswith("Error")

    async def test_toggle_not_found(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_toggle(name="ghost", enabled=False, context=CTX)
        assert result.startswith("Error")

    async def test_remove_ambiguous_name_requires_schedule_id(self, settings, schedule_create):
        """Two schedules sharing a name (bypassing the tool's own duplicate check) must be
        disambiguated via schedule_id rather than silently removing the wrong one."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            store = CronScheduler(settings.db_path)
            await store.add(name="dup", cron_expr="0 8 * * *", action={"type": "custom"}, pincer_user_id="usr_test")
            await store.add(name="dup", cron_expr="0 9 * * *", action={"type": "custom"}, pincer_user_id="usr_test")

            result = await schedule_remove(name="dup", context=CTX)

        assert result.startswith("Error")
        assert "Multiple schedules" in result

    async def test_list_shows_created_schedule_and_tools(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(
                name="a", cron_expr="0 8 * * *", prompt="p", allowed_tools=["websearch__search"], context=CTX
            )
            await schedule_create(name="b", cron_expr="0 9 * * *", prompt="p2", context=CTX)
            result = await schedule_list(context=CTX)
        assert "a —" in result
        assert "tools=websearch__search" in result
        assert "b —" in result
        assert "tools=all" in result

    async def test_remove_by_name(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            result = await schedule_remove(name="a", context=CTX)
            listing = await schedule_list(context=CTX)
        assert "Removed" in result
        assert "no scheduled jobs" in listing.lower()

    async def test_remove_not_found(self, settings):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            result = await schedule_remove(name="ghost", context=CTX)
        assert result.startswith("Error")

    async def test_toggle_by_name(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            result = await schedule_toggle(name="a", enabled=False, context=CTX)
            listing = await schedule_list(context=CTX)
        assert "disabled" in result.lower()
        assert "disabled" in listing

    async def test_remove_by_schedule_id_override(self, settings, schedule_create):
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            from pincer.scheduler.cron import CronScheduler

            store = CronScheduler(settings.db_path)
            existing = await store.list_schedules("usr_test")
            sid = existing[0]["id"]
            result = await schedule_remove(name="a", schedule_id=sid, context=CTX)
        assert "Removed" in result

    async def test_remove_by_schedule_id_rejects_other_users_schedule(self, settings, schedule_create):
        """IDOR regression: an explicit schedule_id must not let one user delete another's schedule."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            store = CronScheduler(settings.db_path)
            sid = (await store.list_schedules("usr_test"))[0]["id"]

            other_ctx = {"pincer_user_id": "usr_attacker", "channel_name": "telegram"}
            result = await schedule_remove(name="whatever", schedule_id=sid, context=other_ctx)

        assert result.startswith("Error")
        assert await store.get(sid) is not None

    async def test_toggle_by_schedule_id_rejects_other_users_schedule(self, settings, schedule_create):
        """IDOR regression: an explicit schedule_id must not let one user toggle another's schedule."""
        with patch("pincer.tools.builtin.schedule_tool.get_settings", return_value=settings):
            await schedule_create(name="a", cron_expr="0 8 * * *", prompt="p", context=CTX)
            store = CronScheduler(settings.db_path)
            sid = (await store.list_schedules("usr_test"))[0]["id"]

            other_ctx = {"pincer_user_id": "usr_attacker", "channel_name": "telegram"}
            result = await schedule_toggle(name="whatever", enabled=False, schedule_id=sid, context=other_ctx)

        assert result.startswith("Error")
        schedules = await store.list_schedules("usr_test")
        assert schedules[0]["enabled"] == 1
