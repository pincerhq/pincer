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

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from google.auth.exceptions import RefreshError

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


def _typed(fake: FakeCalendarService, title: str, start: datetime, event_type: str | None) -> str:
    event_id = _seed(fake, title, start)
    if event_type is not None:
        fake.event("primary", event_id)["eventType"] = event_type
    return event_id


@pytest.mark.parametrize("event_type", ["workingLocation", "outOfOffice", "focusTime"])
async def test_status_events_get_no_reminder(triggers, fake, event_type) -> None:
    """Owner ruling (#11): no daily "Reminder: Home starts at 09:00" for working location,
    out-of-office or focus-time entries."""
    _typed(fake, "Home", _in_minutes(10), event_type)
    listed = fake.events().list(calendarId="primary").execute()["items"]
    assert [e.get("eventType") for e in listed] == [event_type]  # the fake keeps the type

    await _tick(triggers, TICKS)

    assert _sent(triggers) == []


async def test_default_events_are_reminded_with_or_without_an_event_type(triggers, fake) -> None:
    start = _in_minutes(10)
    _typed(fake, "Untyped", start, None)
    _typed(fake, "Typed", start, "default")
    _typed(fake, "Home", start, "workingLocation")

    await _tick(triggers, TICKS)

    sent = _sent(triggers)
    assert len(sent) == 2, sent
    assert sorted("Untyped" in s for s in sent) == [False, True]
    assert sorted("Typed" in s and "Untyped" not in s for s in sent) == [False, True]


async def test_status_events_still_show_in_the_listing(fake_registry, fake) -> None:
    """Only the reminder skips them; list/agenda keep showing the entry."""
    start = _in_minutes(10)
    _typed(fake, "Home", start, "workingLocation")
    day = start.date().isoformat()

    text = await fake_registry.execute("google__list_events", {"time_min": day, "time_max": day})

    assert "Home" in text


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


class _RevocableList:
    """The fake's events().list, failing like a token revoked after startup while ``failing``;
    stops the reminder loop after ``ticks`` list calls."""

    def __init__(self, fake: FakeCalendarService, mgr: EventTriggerManager) -> None:
        self._events = fake.events
        self._mgr = mgr
        self.failing = True
        self.ticks = 0

    def events(self):
        real = self._events()
        proxy = MagicMock()
        proxy.list = lambda **kw: SimpleNamespace(execute=lambda: self._execute(real.list(**kw)))
        return proxy

    def _execute(self, request):
        self.ticks -= 1
        if self.ticks <= 0:
            self._mgr._running = False
        if self.failing:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        return request.execute()


async def _loop(mgr: EventTriggerManager, revocable: _RevocableList, ticks: int) -> None:
    revocable.ticks, mgr._running = ticks, True
    await mgr._calendar_loop()


async def test_failing_reads_warn_once_then_debug_until_a_read_succeeds(triggers, fake, monkeypatch, caplog) -> None:
    """Not a traceback every 60 s (~1440 a day): one WARNING, DEBUG repeats, INFO on recovery."""
    monkeypatch.setattr("pincer.scheduler.triggers.CALENDAR_TICK_S", 0)
    revocable = _RevocableList(fake, triggers)
    monkeypatch.setattr(fake, "events", revocable.events)

    def _levels() -> list[int]:
        found = [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]
        caplog.clear()
        return [r.levelno for r in found]

    with caplog.at_level(logging.DEBUG, logger="pincer.scheduler.triggers"):
        await _loop(triggers, revocable, 3)
        first = [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]
        assert [r.levelno for r in first] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
        assert all(r.exc_info is None for r in first)
        assert "reminders" in first[0].getMessage() and "RefreshError" in first[0].getMessage()
        caplog.clear()

        revocable.failing = False
        await _loop(triggers, revocable, 1)
        assert _levels() == [logging.INFO]

        revocable.failing = True
        await _loop(triggers, revocable, 1)
        assert _levels() == [logging.WARNING]


async def test_an_unexpected_read_error_is_logged_with_its_traceback(triggers, fake, monkeypatch, caplog) -> None:
    """Only Google auth/HTTP errors mean "the calendar is unreachable"; a bug must not hide as a WARNING."""
    monkeypatch.setattr("pincer.scheduler.triggers.CALENDAR_TICK_S", 0)

    async def _broken(*_args, **_kwargs):
        triggers._running = False
        raise ValueError("bad event payload")

    monkeypatch.setattr("pincer.scheduler.triggers.fetch_events", _broken)
    triggers._running = True
    with caplog.at_level(logging.DEBUG, logger="pincer.scheduler.triggers"):
        await triggers._calendar_loop()

    records = [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]
    assert [(r.levelno, r.getMessage()) for r in records] == [(logging.ERROR, "Calendar trigger error")]
    assert records[0].exc_info is not None and records[0].exc_info[0] is ValueError


def _http_error(status: int, reason: str = ""):
    import json

    from googleapiclient.errors import HttpError

    errors = [{"reason": reason, "message": reason}] if reason else []
    body = {"error": {"code": status, "message": f"HTTP {status}", "errors": errors}}
    return HttpError(SimpleNamespace(status=status, reason=f"HTTP {status}"), json.dumps(body).encode())


async def _reminder_tick_failing_with(triggers, monkeypatch, caplog, exc: Exception) -> list[logging.LogRecord]:
    monkeypatch.setattr("pincer.scheduler.triggers.CALENDAR_TICK_S", 0)

    async def _failing(*_args, **_kwargs):
        triggers._running = False
        raise exc

    monkeypatch.setattr("pincer.scheduler.triggers.fetch_events", _failing)
    triggers._running = True
    with caplog.at_level(logging.DEBUG, logger="pincer.scheduler.triggers"):
        await triggers._calendar_loop()
    return [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (503, ""),
        (500, ""),
        (429, ""),
        (403, "rateLimitExceeded"),
        (403, "userRateLimitExceeded"),
        (403, "quotaExceeded"),  # PR #316 review: the daily quota is an outage until it resets
        (403, "dailyLimitExceeded"),
    ],
)
async def test_a_google_outage_still_pauses_reminders_quietly(triggers, fake, monkeypatch, caplog, status, reason):
    """Google down or rate-limiting us: one WARNING, no traceback."""
    records = await _reminder_tick_failing_with(triggers, monkeypatch, caplog, _http_error(status, reason))

    assert [r.levelno for r in records] == [logging.WARNING]
    assert "Calendar reminders paused" in records[0].getMessage() and records[0].exc_info is None


@pytest.mark.parametrize(("status", "reason"), [(400, "badRequest"), (404, "notFound"), (403, "forbidden")])
async def test_a_client_side_google_error_is_logged_with_its_traceback(
    triggers, fake, monkeypatch, caplog, status, reason
):
    """PR #316 review: a 4xx (a malformed timeMin, a missing calendar) is a bug, not an outage — it must
    not stop reminders behind one quiet WARNING."""
    from googleapiclient.errors import HttpError

    records = await _reminder_tick_failing_with(triggers, monkeypatch, caplog, _http_error(status, reason))

    assert [(r.levelno, r.getMessage()) for r in records] == [(logging.ERROR, "Calendar trigger error")]
    assert records[0].exc_info is not None and records[0].exc_info[0] is HttpError


async def test_a_token_revoked_after_the_service_cache_expires_warns_once_without_a_traceback(
    triggers, fake, google_token, mock_credentials, monkeypatch, caplog
) -> None:
    """Past the factory's 30-minute service cache every tick refreshes the token through GoogleAuth,
    which wraps Google's RefreshError in its own error: still an outage, not a traceback a minute."""
    import json

    from google.oauth2.credentials import Credentials

    monkeypatch.setattr("pincer.scheduler.triggers.CALENDAR_TICK_S", 0)
    factory = triggers._calendar.factory()
    assert factory is not None  # precondition: the real Workspace factory, not a stub
    _creds_path, token_path = mock_credentials
    token = json.loads(token_path.read_text())
    token_path.write_text(json.dumps({**token, "expiry": "2000-01-01T00:00:00Z"}))
    factory.invalidate()  # the cached service has expired
    factory.auth._credentials = None
    refreshes = {"n": 0}

    def _revoked(_self, _request):
        refreshes["n"] += 1
        if refreshes["n"] >= 2:
            triggers._running = False
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", _revoked)
    triggers._running = True
    with caplog.at_level(logging.DEBUG, logger="pincer.scheduler.triggers"):
        await triggers._calendar_loop()

    assert refreshes["n"] == 2  # precondition: both ticks really went through GoogleAuth's refresh
    records = [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
    assert all(r.exc_info is None for r in records)
    assert "GoogleTokenError" in records[0].getMessage() and "invalid_grant" in records[0].getMessage()


async def test_a_dropped_connection_warns_once_then_debug_without_a_traceback(
    triggers, fake, monkeypatch, caplog
) -> None:
    """A connection to Google timing out is an outage like a revoked token, not a bug."""
    monkeypatch.setattr("pincer.scheduler.triggers.CALENDAR_TICK_S", 0)
    calls = {"n": 0}

    async def _timeout(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            triggers._running = False
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("pincer.scheduler.triggers.fetch_events", _timeout)
    triggers._running = True
    with caplog.at_level(logging.DEBUG, logger="pincer.scheduler.triggers"):
        await triggers._calendar_loop()

    records = [r for r in caplog.records if r.name == "pincer.scheduler.triggers"]
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert all(r.exc_info is None for r in records)
    assert "TimeoutError" in records[0].getMessage()


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
