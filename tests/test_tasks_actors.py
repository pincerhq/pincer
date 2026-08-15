"""Tests for pincer.tasks.actors — repid actor bodies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from pincer.scheduler.cron import CronScheduler
from pincer.tasks.actors import process_webhook, run_scheduled_action
from pincer.tasks.context import set_context


@pytest_asyncio.fixture
async def store(tmp_path):
    sched = CronScheduler(tmp_path / "pincer.db")
    await sched.ensure_table()
    return sched


def _fake_settings(tmp_path, task_max_retries=1):
    return SimpleNamespace(db_path=tmp_path / "pincer.db", task_max_retries=task_max_retries)


@pytest.mark.asyncio
class TestRunScheduledAction:
    async def test_briefing_result_sent_to_user(self, store, tmp_path):
        sid = await store.add("morning", "0 7 * * *", {"type": "briefing"}, "usr_test")

        router = AsyncMock()
        proactive = AsyncMock()
        proactive.generate_briefing = AsyncMock(return_value="Good morning!")
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)):
            await run_scheduled_action(schedule_id=sid)

        proactive.generate_briefing.assert_awaited_once()
        router.send_to_user.assert_awaited_once()
        args, _ = router.send_to_user.call_args
        assert args[0] == "usr_test"
        assert args[1] == "Good morning!"

    async def test_missing_schedule_is_noop(self, store, tmp_path):
        router = AsyncMock()
        proactive = AsyncMock()
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)):
            await run_scheduled_action(schedule_id=999999)

        router.send_to_user.assert_not_awaited()

    async def test_delivery_failure_logged_as_error_not_delivered(self, store, tmp_path, caplog):
        """send_to_user returning False must not be logged/treated as a success."""
        sid = await store.add("morning", "0 7 * * *", {"type": "briefing"}, "usr_test")

        router = AsyncMock()
        router.send_to_user = AsyncMock(return_value=False)
        proactive = AsyncMock()
        proactive.generate_briefing = AsyncMock(return_value="Good morning!")
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with (
            patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)),
            caplog.at_level("ERROR", logger="pincer.tasks.actors"),
        ):
            await run_scheduled_action(schedule_id=sid)

        assert any("NOT delivered" in r.message for r in caplog.records)
        assert not any("delivered:" in r.message for r in caplog.records)

    async def test_unknown_action_type_is_noop(self, store, tmp_path):
        sid = await store.add("weird", "0 7 * * *", {"type": "unknown"}, "usr_test")

        router = AsyncMock()
        proactive = AsyncMock()
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)):
            await run_scheduled_action(schedule_id=sid)

        router.send_to_user.assert_not_awaited()

    async def test_handler_failure_reraises_after_retries(self, store, tmp_path, caplog):
        """A handler that keeps failing propagates so repid's on_error='nack' can ack-and-DLQ it."""
        sid = await store.add("morning", "0 7 * * *", {"type": "briefing"}, "usr_test")

        router = AsyncMock()
        proactive = AsyncMock()
        proactive.generate_briefing = AsyncMock(side_effect=RuntimeError("boom"))
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with (
            patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)),
            caplog.at_level("ERROR", logger="pincer.tasks.actors"),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_scheduled_action(schedule_id=sid)

        assert any("failed after retries" in r.message for r in caplog.records)
        router.send_to_user.assert_not_awaited()

    async def test_handler_retries_then_succeeds(self, store, tmp_path):
        """With task_max_retries > 1, tenacity must actually retry, not just fail once."""
        sid = await store.add("morning", "0 7 * * *", {"type": "briefing"}, "usr_test")

        router = AsyncMock()
        proactive = AsyncMock()
        proactive.generate_briefing = AsyncMock(
            side_effect=[RuntimeError("transient"), RuntimeError("transient"), "Good morning!"]
        )
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path, task_max_retries=3)):
            await run_scheduled_action(schedule_id=sid)

        assert proactive.generate_briefing.await_count == 3
        router.send_to_user.assert_awaited_once()

    async def test_non_string_result_logged_as_no_message(self, store, tmp_path, caplog):
        """A handler returning None (e.g. custom action) is completed without delivery."""
        sid = await store.add("job", "0 7 * * *", {"type": "custom"}, "usr_test")

        router = AsyncMock()
        proactive = AsyncMock()
        proactive.run_custom_action = AsyncMock(return_value=None)
        triggers = AsyncMock()
        set_context(router, proactive, triggers)

        with (
            patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)),
            caplog.at_level("INFO", logger="pincer.tasks.actors"),
        ):
            await run_scheduled_action(schedule_id=sid)

        router.send_to_user.assert_not_awaited()
        assert any("no message to send" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestProcessWebhook:
    async def test_delegates_to_triggers(self, tmp_path):
        router = AsyncMock()
        proactive = AsyncMock()
        triggers = AsyncMock()
        triggers.handle_webhook = AsyncMock(return_value="ok")
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)):
            await process_webhook(webhook_id="wh_1", payload={"a": 1}, pincer_user_id="usr_test")

        triggers.handle_webhook.assert_awaited_once_with("wh_1", {"a": 1}, "usr_test")

    async def test_handler_failure_reraises_after_retries(self, tmp_path, caplog):
        router = AsyncMock()
        proactive = AsyncMock()
        triggers = AsyncMock()
        triggers.handle_webhook = AsyncMock(side_effect=RuntimeError("webhook boom"))
        set_context(router, proactive, triggers)

        with (
            patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path)),
            caplog.at_level("ERROR", logger="pincer.tasks.actors"),
            pytest.raises(RuntimeError, match="webhook boom"),
        ):
            await process_webhook(webhook_id="wh_1", payload={"a": 1}, pincer_user_id="usr_test")

        assert any("failed after retries" in r.message for r in caplog.records)

    async def test_handler_retries_then_succeeds(self, tmp_path):
        """With task_max_retries > 1, tenacity must actually retry, not just fail once."""
        router = AsyncMock()
        proactive = AsyncMock()
        triggers = AsyncMock()
        triggers.handle_webhook = AsyncMock(side_effect=[RuntimeError("transient"), RuntimeError("transient"), "ok"])
        set_context(router, proactive, triggers)

        with patch("pincer.tasks.actors.get_settings", return_value=_fake_settings(tmp_path, task_max_retries=3)):
            await process_webhook(webhook_id="wh_1", payload={"a": 1}, pincer_user_id="usr_test")

        assert triggers.handle_webhook.await_count == 3
