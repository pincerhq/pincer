"""Tests for EventTriggerManager (scheduler/triggers.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from pincer.scheduler.triggers import EventTriggerManager


@pytest_asyncio.fixture
async def trigger_mgr(tmp_path):
    db = tmp_path / "triggers.db"
    router = AsyncMock()
    mgr = EventTriggerManager(db_path=db, router=router)
    await mgr.ensure_table()
    return mgr


@pytest.mark.asyncio
class TestEnsureTable:
    async def test_creates_table(self, tmp_path):
        import aiosqlite

        db = tmp_path / "t.db"
        mgr = EventTriggerManager(db_path=db, router=AsyncMock())
        await mgr.ensure_table()

        async with aiosqlite.connect(str(db)) as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='event_triggers'"
            )
        assert rows


@pytest.mark.asyncio
class TestDeduplication:
    async def test_not_processed_initially(self, trigger_mgr):
        result = await trigger_mgr._is_processed("new_email", "uid_001")
        assert result is False

    async def test_mark_and_check(self, trigger_mgr):
        await trigger_mgr._mark_processed("new_email", "uid_001", "user_1", "notified")
        assert await trigger_mgr._is_processed("new_email", "uid_001") is True

    async def test_different_keys_not_confused(self, trigger_mgr):
        await trigger_mgr._mark_processed("new_email", "uid_001", "user_1")
        assert await trigger_mgr._is_processed("new_email", "uid_002") is False

    async def test_different_types_not_confused(self, trigger_mgr):
        await trigger_mgr._mark_processed("new_email", "key_1", "user_1")
        assert await trigger_mgr._is_processed("webhook", "key_1") is False

    async def test_mark_idempotent(self, trigger_mgr):
        await trigger_mgr._mark_processed("new_email", "uid_dup", "u1")
        await trigger_mgr._mark_processed("new_email", "uid_dup", "u1")
        assert await trigger_mgr._is_processed("new_email", "uid_dup") is True


@pytest.mark.asyncio
class TestHandleWebhook:
    async def test_first_call_sends_notification(self, trigger_mgr):
        payload = {"source": "GitHub", "event": "push", "summary": "main branch updated"}
        result = await trigger_mgr.handle_webhook("wh_01", payload, "user_1")
        assert "GitHub" in result
        assert "push" in result
        trigger_mgr._router.send_to_user.assert_awaited_once()

    async def test_duplicate_call_skipped(self, trigger_mgr):
        payload = {"id": "evt_fixed", "source": "Stripe", "event": "payment"}
        await trigger_mgr.handle_webhook("wh_02", payload, "user_1")
        result = await trigger_mgr.handle_webhook("wh_02", payload, "user_1")
        assert result == "Already processed"
        assert trigger_mgr._router.send_to_user.await_count == 1

    async def test_missing_fields_use_defaults(self, trigger_mgr):
        payload = {}
        result = await trigger_mgr.handle_webhook("wh_03", payload, "user_1")
        assert "Unknown" in result
        assert "notification" in result

    async def test_payload_summary_truncated_for_large_payload(self, trigger_mgr):
        big_payload = {"data": "x" * 1000, "id": "big_evt"}
        result = await trigger_mgr.handle_webhook("wh_04", big_payload, "user_1")
        assert len(result) < 2000


@pytest.mark.asyncio
class TestStartStop:
    async def test_stop_cancels_tasks(self, tmp_path):
        db = tmp_path / "s.db"
        router = AsyncMock()
        mgr = EventTriggerManager(db_path=db, router=router)

        fake_settings = MagicMock()
        fake_settings.email_imap_host = ""
        fake_settings.email_username = ""
        fake_settings.default_user_id = "u1"

        with patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings):
            await mgr.start()
            assert mgr._running is True
            assert len(mgr._tasks) == 1  # only calendar loop (no email config)
            await mgr.stop()

        assert mgr._running is False
        assert len(mgr._tasks) == 0

    async def test_start_with_email_creates_two_tasks(self, tmp_path):
        db = tmp_path / "s2.db"
        router = AsyncMock()
        mgr = EventTriggerManager(db_path=db, router=router)

        fake_settings = MagicMock()
        fake_settings.email_imap_host = "imap.example.com"
        fake_settings.email_username = "user@example.com"
        fake_settings.default_user_id = "u1"

        with patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings):
            await mgr.start()
            assert len(mgr._tasks) == 2
            await mgr.stop()


@pytest.mark.asyncio
class TestCheckNewEmails:
    async def test_no_user_id_returns_early(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = ""

        with patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings):
            await trigger_mgr._check_new_emails()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_error_result_skipped(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.email_tool.email_check", return_value="Error: connection refused"),
        ):
            await trigger_mgr._check_new_emails()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_no_unread_skipped(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.email_tool.email_check", return_value="No unread messages"),
        ):
            await trigger_mgr._check_new_emails()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_first_run_learns_uids(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"
        email_result = "- UID: 101 Subject: Hello\n- UID: 102 Subject: World"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.email_tool.email_check", return_value=email_result),
        ):
            await trigger_mgr._check_new_emails()

        assert trigger_mgr._seen_email_uids == {"101", "102"}
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_new_uid_triggers_notification(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"

        trigger_mgr._seen_email_uids = {"101"}
        email_result = "- UID: 101 Subject: Old\n- UID: 103 Subject: New"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.email_tool.email_check", return_value=email_result),
        ):
            await trigger_mgr._check_new_emails()

        trigger_mgr._router.send_to_user.assert_awaited_once()
        call_args = trigger_mgr._router.send_to_user.call_args
        assert call_args[0][0] == "u1"
        assert "1 new email" in call_args[0][1]


@pytest.mark.asyncio
class TestCheckUpcoming:
    async def test_no_user_id_skips(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = ""

        with patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings):
            await trigger_mgr._check_upcoming()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_calendar_error_skips(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.calendar_tool.calendar_today", return_value="Error: no credentials"),
        ):
            await trigger_mgr._check_upcoming()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_clear_calendar_skips(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.calendar_tool.calendar_today", return_value="Your calendar is clear today"),
        ):
            await trigger_mgr._check_upcoming()
        trigger_mgr._router.send_to_user.assert_not_awaited()

    async def test_upcoming_event_sends_notification(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"
        calendar_result = "10:00 Team standup\n11:00 1:1 with Alice"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.calendar_tool.calendar_today", return_value=calendar_result),
        ):
            await trigger_mgr._check_upcoming()

        trigger_mgr._router.send_to_user.assert_awaited_once()
        args = trigger_mgr._router.send_to_user.call_args[0]
        assert args[0] == "u1"
        assert "reminder" in args[1].lower()

    async def test_same_hour_not_sent_twice(self, trigger_mgr):
        fake_settings = MagicMock()
        fake_settings.default_user_id = "u1"
        calendar_result = "10:00 Team standup"

        with (
            patch("pincer.scheduler.triggers.get_settings", return_value=fake_settings),
            patch("pincer.tools.builtin.calendar_tool.calendar_today", return_value=calendar_result),
        ):
            await trigger_mgr._check_upcoming()
            await trigger_mgr._check_upcoming()

        assert trigger_mgr._router.send_to_user.await_count == 1
