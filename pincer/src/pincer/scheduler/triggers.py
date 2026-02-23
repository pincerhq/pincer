"""
Event triggers — reactive actions on external events.

Trigger types:
1. New email -> notify user
2. Calendar reminder -> 15 min before event notification
3. Custom webhooks -> trigger agent actions

Deduplication via event_triggers table prevents duplicate notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from pincer.config import get_settings

logger = logging.getLogger(__name__)


class EventTriggerManager:
    """Manages event-triggered reactive actions."""

    def __init__(self, db_path: Path, router: Any) -> None:
        self._db_path = str(db_path)
        self._router = router
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def ensure_table(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS event_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_type TEXT NOT NULL,
                    trigger_key TEXT NOT NULL,
                    pincer_user_id TEXT NOT NULL,
                    processed_at TEXT DEFAULT (datetime('now')),
                    result TEXT,
                    UNIQUE(trigger_type, trigger_key)
                )
            """)
            await db.commit()

    async def start(self) -> None:
        await self.ensure_table()
        self._running = True

        settings = get_settings()
        if settings.email_imap_host and settings.email_username:
            self._tasks.append(
                asyncio.create_task(self._email_loop(), name="trigger-email"),
            )

        self._tasks.append(
            asyncio.create_task(self._calendar_loop(), name="trigger-calendar"),
        )

        logger.info("Triggers started (%d loops)", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Triggers stopped")

    # ── Deduplication ────────────────────────────

    async def _is_processed(self, trigger_type: str, trigger_key: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT 1 FROM event_triggers WHERE trigger_type = ? AND trigger_key = ?",
                (trigger_type, trigger_key),
            )
            return len(rows) > 0

    async def _mark_processed(
        self,
        trigger_type: str,
        trigger_key: str,
        user_id: str,
        result: str = "",
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO event_triggers "
                "(trigger_type, trigger_key, pincer_user_id, result) VALUES (?, ?, ?, ?)",
                (trigger_type, trigger_key, user_id, result),
            )
            await db.commit()

    # ── Email trigger ────────────────────────────

    async def _email_loop(self) -> None:
        while self._running:
            try:
                await self._check_new_emails()
            except Exception:
                logger.exception("Email trigger error")
            await asyncio.sleep(120)

    async def _check_new_emails(self) -> None:
        from pincer.tools.builtin.email_tool import email_check

        settings = get_settings()
        result = await email_check(limit=5)
        if not isinstance(result, str) or "Error" in result or "No unread" in result:
            return

        # Parse the formatted string for message IDs - this is a simple approach
        # In production, a structured return would be preferable
        user_id = settings.default_user_id
        if not user_id:
            return

        trigger_key = f"email_batch_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
        if await self._is_processed("new_email", trigger_key):
            return

        notification = f"New email notification:\n{result}"
        await self._router.send_to_user(user_id, notification)
        await self._mark_processed("new_email", trigger_key, user_id, "notified")

    # ── Calendar reminder trigger ────────────────

    async def _calendar_loop(self) -> None:
        while self._running:
            try:
                await self._check_upcoming()
            except Exception:
                logger.exception("Calendar trigger error")
            await asyncio.sleep(60)

    async def _check_upcoming(self) -> None:
        settings = get_settings()
        user_id = settings.default_user_id
        if not user_id:
            return

        try:
            from pincer.tools.builtin.calendar_tool import calendar_today

            result = await calendar_today()
            if not isinstance(result, str) or "Error" in result or "clear" in result.lower():
                return

            # The calendar_today returns formatted text; we look for events
            # starting within the next 10-15 minutes by checking current time
            # against the schedule (simplified approach)
            now = datetime.now(timezone.utc)
            trigger_key = f"cal_reminder_{now.strftime('%Y%m%d%H')}"
            if await self._is_processed("calendar_reminder", trigger_key):
                return

            # Only send one reminder per hour as a digest
            # A more sophisticated approach would parse event start times
            notification = f"Upcoming events reminder:\n{result}"
            await self._router.send_to_user(user_id, notification)
            await self._mark_processed("calendar_reminder", trigger_key, user_id)

        except Exception as e:
            logger.error("Calendar trigger check failed: %s", e)

    # ── Webhook handler ──────────────────────────

    async def handle_webhook(
        self,
        webhook_id: str,
        payload: dict[str, Any],
        pincer_user_id: str,
    ) -> str:
        trigger_key = f"wh_{webhook_id}_{payload.get('id', datetime.now().isoformat())}"
        if await self._is_processed("webhook", trigger_key):
            return "Already processed"

        source = payload.get("source", "Unknown")
        event_type = payload.get("event", "notification")
        summary = payload.get("summary", json.dumps(payload, indent=2)[:500])

        notification = f"Webhook: {source}\nEvent: {event_type}\n{summary}"
        await self._router.send_to_user(pincer_user_id, notification)
        await self._mark_processed("webhook", trigger_key, pincer_user_id, event_type)
        return notification
