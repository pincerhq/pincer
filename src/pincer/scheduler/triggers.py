"""
Event triggers — reactive actions on external events.

Trigger types:
1. New email -> notify user
2. Calendar reminder -> one message per event starting within 15 minutes
3. Custom webhooks -> trigger agent actions

Deduplication via event_triggers table prevents duplicate notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httplib2
from google.auth.exceptions import GoogleAuthError

from pincer.config import get_settings
from pincer.core.log_once import LogOnce
from pincer.db.engine import get_database_url
from pincer.integrations.google.auth import GoogleTokenError
from pincer.integrations.google.calendar_render import render_reminder, start_instant
from pincer.integrations.google.calendar_source import CalendarSource
from pincer.integrations.google.quota import is_google_outage
from pincer.integrations.google.tools_calendar import fetch_events
from pincer.services.scheduler import EventTriggerService

_UID_RE = re.compile(r"UID:\s*(\d+)")

# Upcoming-event reminder (Q11): checked every tick, one message per event.
CALENDAR_TICK_S = 60
REMINDER_WINDOW = timedelta(minutes=15)
REMINDER_TRIGGER = "calendar_reminder"
REMINDER_CALENDAR = "primary"
# Status entries, not meetings (owner ruling, #297): no "Reminder: Home starts at 09:00".
# A missing eventType means "default" and is reminded.
NO_REMINDER_EVENT_TYPES = frozenset({"workingLocation", "outOfOffice", "focusTime"})
# A calendar read failing with these is an outage (token revoked — raw, or wrapped by
# GoogleAuth once the service cache expires — or the network down): logged once, quietly
# retried. A Google HTTP error is an outage only when Google is down or limiting us
# (quota.is_google_outage); anything else, a 4xx included, is a bug and gets a traceback.
CALENDAR_OUTAGE_ERRORS = (GoogleAuthError, GoogleTokenError, OSError, httplib2.HttpLib2Error)

logger = logging.getLogger(__name__)


def is_calendar_outage(exc: BaseException) -> bool:
    """True when a calendar read failed because Google or the way to it is down, not because of a bug."""
    return isinstance(exc, CALENDAR_OUTAGE_ERRORS) or is_google_outage(exc)


class EventTriggerManager:
    """Manages event-triggered reactive actions."""

    def __init__(self, db_path: Path, router: Any) -> None:
        self._db_path = str(db_path)
        self._router = router
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._seen_email_uids: set[str] | None = None
        self._dedupe: EventTriggerService | None = None
        self._calendar = CalendarSource("upcoming-event reminders")
        self._calendar_read_log = LogOnce(logger)

    async def ensure_table(self) -> None:
        """Ensure the event_triggers table is at head (see pincer.db.migrations)."""
        self._dedupe = await EventTriggerService.for_path(Path(self._db_path))

    @property
    def _service(self) -> EventTriggerService:
        if self._dedupe is None:
            self._dedupe = EventTriggerService(get_database_url(Path(self._db_path)))
        return self._dedupe

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
        return await self._service.is_processed(trigger_type, trigger_key)

    async def _mark_processed(
        self,
        trigger_type: str,
        trigger_key: str,
        user_id: str,
        result: str = "",
    ) -> None:
        await self._service.mark_processed(trigger_type, trigger_key, user_id, result)

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
        user_id = settings.default_user_id
        if not user_id:
            return

        result = await email_check(limit=10)
        if not isinstance(result, str) or "Error" in result or "No unread" in result:
            return

        current_uids = set(_UID_RE.findall(result))
        if not current_uids:
            return

        if self._seen_email_uids is None:
            self._seen_email_uids = current_uids
            logger.debug("Email trigger: learned %d existing UIDs", len(current_uids))
            return

        new_uids = current_uids - self._seen_email_uids
        self._seen_email_uids = current_uids

        if not new_uids:
            return

        new_lines = []
        for line_block in result.split("- UID: "):
            for uid in new_uids:
                if line_block.startswith(uid):
                    new_lines.append(f"- UID: {line_block.strip()}")
                    break

        notification = f"You have {len(new_uids)} new email(s):\n" + "\n".join(new_lines)
        await self._router.send_to_user(user_id, notification)

        trigger_key = f"email_uids_{'_'.join(sorted(new_uids))}"
        await self._mark_processed("new_email", trigger_key, user_id, "notified")

    # ── Calendar reminder trigger ────────────────

    async def _calendar_loop(self) -> None:
        while self._running:
            try:
                await self._check_upcoming()
            except Exception:
                logger.exception("Calendar trigger error")
            await asyncio.sleep(CALENDAR_TICK_S)

    async def _check_upcoming(self) -> None:
        """Remind about each event starting within the window, once per (event, start).

        Keyed on event ID plus start time, so a moved event is reminded again at
        its new time; the key is persisted, so a restart does not repeat one.
        All-day, cancelled and self-declined events, and working-location,
        out-of-office and focus-time entries get no reminder.

        A calendar outage (a token revoked after startup, Google down or
        rate-limiting, a dropped connection) logs one WARNING, then DEBUG on every tick until
        a read succeeds again; any other error propagates to the loop's
        ``logger.exception``.
        """
        user_id = get_settings().default_user_id
        if not user_id:
            return
        factory = self._calendar.factory()
        if factory is None:
            return

        now = datetime.now(UTC)
        horizon = now + REMINDER_WINDOW
        try:
            page = await fetch_events(factory, REMINDER_CALENDAR, now.isoformat(), horizon.isoformat())
        except Exception as exc:
            if not is_calendar_outage(exc):
                raise
            self._calendar_read_log.log(
                "Calendar reminders paused: reading the calendar failed (%s: %s); retrying every %d s",
                type(exc).__name__,
                exc,
                CALENDAR_TICK_S,
            )
            return
        if self._calendar_read_log.clear():
            logger.info("Calendar reminders resumed: the calendar can be read again")
        for event in page.events:
            start = start_instant(event)
            if start is None or not (now <= start <= horizon) or not _wants_reminder(event):
                continue
            key = f"{event.get('id', '')}@{start.astimezone(UTC).isoformat()}"
            if await self._is_processed(REMINDER_TRIGGER, key):
                continue
            await self._router.send_to_user(user_id, render_reminder(event, page.zone, now))
            await self._mark_processed(REMINDER_TRIGGER, key, user_id, "reminded")

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


def _wants_reminder(event: dict[str, Any]) -> bool:
    """A normal event: not a status entry, not cancelled, not declined by the calendar's owner."""
    if event.get("eventType") in NO_REMINDER_EVENT_TYPES:
        return False
    if event.get("status") == "cancelled":
        return False
    return not any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees") or [])
