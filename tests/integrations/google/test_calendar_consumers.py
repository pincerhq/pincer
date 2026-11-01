"""
Calendar consumers on the Workspace path (calendar reliability, slice E).

The morning briefing and the upcoming-event reminder read structured events
through the Google service factory — the real ``get_google_factory`` path, with
only ``googleapiclient.discovery.build`` swapped for the stateful fake — so the
``[integrations.google]`` switch is honoured exactly as for the tools.

Reminder rules (Q11): on each 60-second tick, events starting within the next
15 minutes get one message each, once — deduplicated on event ID plus start
time, so a moved event is reminded again at its new time. All-day, cancelled
and declined events are skipped; times are shown in the calendar's zone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from integrations.google.fake_calendar import FakeCalendarService
from pincer.integrations.google.config import GoogleIntegrationConfig
from pincer.scheduler.proactive import ProactiveAgent
from pincer.scheduler.triggers import EventTriggerManager

KYIV = ZoneInfo("Europe/Kyiv")
USER = "u1"
TICKS = 5


@pytest.fixture
def fake() -> FakeCalendarService:
    return FakeCalendarService({"primary": "Europe/Kyiv"})


@pytest.fixture
def owner(monkeypatch):
    settings = MagicMock()
    settings.default_user_id = USER
    settings.email_imap_host = ""
    monkeypatch.setattr("pincer.scheduler.triggers.get_settings", lambda: settings)
    return settings


@pytest_asyncio.fixture
async def triggers(tmp_path, google_token, owner):
    mgr = EventTriggerManager(db_path=tmp_path / "triggers.db", router=AsyncMock())
    await mgr.ensure_table()
    return mgr


@pytest_asyncio.fixture
async def briefing(tmp_path, google_token):
    agent = ProactiveAgent(tmp_path / "briefing.db")
    await agent.ensure_table()
    yield agent
    await agent.close()


def _in_minutes(minutes: int) -> datetime:
    """A whole-minute instant ``minutes`` from now, in Kyiv."""
    moment = (datetime.now(UTC) + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    return moment.astimezone(KYIV)


def _seed(fake: FakeCalendarService, title: str, start: datetime, minutes: int = 30) -> str:
    return fake.seed_event("primary", title, start.isoformat(), (start + timedelta(minutes=minutes)).isoformat())


def _sent(mgr: EventTriggerManager) -> list[str]:
    return [call.args[1] for call in mgr._router.send_to_user.await_args_list]


async def _tick(mgr: EventTriggerManager, times: int = 1) -> None:
    for _ in range(times):
        await mgr._check_upcoming()


# ── Reminder ─────────────────────────────────────────────────────────────────


async def test_event_in_ten_minutes_is_reminded_exactly_once(triggers, fake) -> None:
    start = _in_minutes(10)
    _seed(fake, "Standup", start)

    await _tick(triggers, TICKS)

    sent = _sent(triggers)
    assert len(sent) == 1, sent
    assert "Standup" in sent[0]
    assert start.strftime("%H:%M") in sent[0]  # the calendar's (Kyiv) wall clock
    assert "Europe/Kyiv" in sent[0]
    assert {call.args[0] for call in triggers._router.send_to_user.await_args_list} == {USER}


async def test_event_titled_error_is_reminded(triggers, fake) -> None:
    _seed(fake, "Error", _in_minutes(5))

    await _tick(triggers, TICKS)

    sent = _sent(triggers)
    assert len(sent) == 1, sent
    assert "Error" in sent[0]


async def test_each_upcoming_event_gets_its_own_reminder(triggers, fake) -> None:
    _seed(fake, "Standup", _in_minutes(4))
    _seed(fake, "Dentist", _in_minutes(12))

    await _tick(triggers, TICKS)

    sent = _sent(triggers)
    assert len(sent) == 2, sent
    assert sorted("Standup" in s for s in sent) == [False, True]
    assert sorted("Dentist" in s for s in sent) == [False, True]


async def test_no_repeat_after_a_restart(tmp_path, triggers, fake) -> None:
    """Dedup is persisted: a new manager on the same database does not remind again."""
    _seed(fake, "Standup", _in_minutes(8))
    await _tick(triggers, 2)
    assert len(_sent(triggers)) == 1

    restarted = EventTriggerManager(db_path=tmp_path / "triggers.db", router=AsyncMock())
    await restarted.ensure_table()
    await _tick(restarted, TICKS)

    assert fake.sent("events.list"), "the restarted reminder never read the calendar"
    assert _sent(restarted) == []


async def test_moved_event_is_reminded_again_at_its_new_time(triggers, fake) -> None:
    event_id = _seed(fake, "Standup", _in_minutes(5))
    await _tick(triggers, TICKS)
    assert len(_sent(triggers)) == 1

    later = _in_minutes(13)
    body = {**fake.event("primary", event_id)}
    body["start"] = {"dateTime": later.isoformat()}
    body["end"] = {"dateTime": (later + timedelta(minutes=30)).isoformat()}
    fake.events().update(calendarId="primary", eventId=event_id, body=body).execute()
    await _tick(triggers, TICKS)

    sent = _sent(triggers)
    assert len(sent) == 2, sent
    assert later.strftime("%H:%M") in sent[1]


async def test_all_day_cancelled_declined_and_later_events_get_no_reminder(triggers, fake) -> None:
    today = datetime.now(KYIV).date()
    fake.seed_all_day("primary", "Holiday", today.isoformat(), (today + timedelta(days=1)).isoformat())
    cancelled = _seed(fake, "Cancelled sync", _in_minutes(6))
    fake.event("primary", cancelled)["status"] = "cancelled"
    declined = _seed(fake, "Declined review", _in_minutes(7))
    fake.event("primary", declined)["attendees"] = [
        {"email": "boss@example.com", "responseStatus": "accepted"},
        {"email": "me@example.com", "self": True, "responseStatus": "declined"},
    ]
    _seed(fake, "Lunch", _in_minutes(40))

    await _tick(triggers, TICKS)

    assert fake.sent("events.list"), "the reminder never read the calendar"  # not a vacuous pass
    assert _sent(triggers) == []


async def test_no_whole_day_digest_is_sent(triggers, fake) -> None:
    _seed(fake, "Board meeting", _in_minutes(120))
    _seed(fake, "Dinner", _in_minutes(300))

    await _tick(triggers, TICKS)

    assert fake.sent("events.list"), "the reminder never read the calendar"
    assert _sent(triggers) == []


async def test_reminder_reads_nothing_when_google_is_switched_off(triggers, fake, google_token) -> None:
    google_token.config = GoogleIntegrationConfig(enabled=False)
    _seed(fake, "Standup", _in_minutes(10))

    await _tick(triggers, TICKS)

    assert fake.calls == []
    assert _sent(triggers) == []


async def test_reminder_reads_nothing_when_calendar_is_not_a_configured_service(triggers, fake, google_token) -> None:
    google_token.config = GoogleIntegrationConfig(services=["gmail", "drive"])
    _seed(fake, "Standup", _in_minutes(10))

    await _tick(triggers, TICKS)

    assert fake.calls == []
    assert _sent(triggers) == []


# ── Briefing ─────────────────────────────────────────────────────────────────


async def test_briefing_lists_todays_events_in_the_calendar_zone(briefing, fake) -> None:
    today = datetime.now(KYIV).date()
    start = datetime.combine(today, datetime.min.time(), KYIV).replace(hour=10)
    _seed(fake, "Standup", start, minutes=60)
    fake.seed_all_day("primary", "Holiday", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(fake, "Tomorrow's review", start + timedelta(days=1), minutes=60)

    section = await briefing._calendar()

    assert "Standup" in section
    assert "10:00" in section and "11:00" in section  # Kyiv wall clock
    assert "09:00" not in section  # not converted to the Berlin deployment clock
    assert "Europe/Kyiv" in section
    assert "Holiday" in section and "all day" in section.lower()
    assert "Tomorrow's review" not in section


async def test_briefing_reads_nothing_when_google_is_switched_off(briefing, fake, google_token) -> None:
    google_token.config = GoogleIntegrationConfig(enabled=False)
    _seed(fake, "Standup", _in_minutes(60))

    section = await briefing._calendar()

    assert fake.calls == []
    assert "Standup" not in section
