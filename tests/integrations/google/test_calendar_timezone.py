"""
Calendar times follow the calendar's timezone (calendar reliability, slice A).

Driven through the real calendar tools against the stateful fake Calendar
service: tool calls in, stored event times / bounds actually sent out. The
live failure these pin: a Kyiv calendar on a Berlin deployment stored an
offset-less "10:00" as 11:00 Kyiv, and an offset-less list bound got HTTP 400.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.tools_calendar import (
    google__check_freebusy,
    google__create_event,
    google__list_events,
    google__search_events,
    google__update_event,
    register_calendar_tools,
)
from pincer.tools.registry import ToolRegistry

KYIV = ZoneInfo("Europe/Kyiv")
NEW_YORK = ZoneInfo("America/New_York")
WORK_CAL = "work@group.calendar.google.com"


@pytest.fixture(autouse=True)
def berlin_deployment(monkeypatch):
    """PINCER_TIMEZONE=Europe/Berlin — the deployment zone, not the calendar's."""
    monkeypatch.setattr(
        "pincer.config.get_settings_relaxed",
        lambda: SimpleNamespace(timezone="Europe/Berlin", voice_timezone="Europe/Berlin"),
    )


@pytest.fixture
def fake():
    return FakeCalendarService({"primary": "Europe/Kyiv", WORK_CAL: "America/New_York"})


@pytest.fixture
def factory(fake):
    return FakeCalendarFactory(fake)


@pytest.fixture
def registry(factory):
    reg = ToolRegistry()
    register_calendar_tools(reg, factory)
    return reg


def _seed_kyiv_day_edges(fake: FakeCalendarService) -> None:
    """One event just inside the local 12th (UTC 11th) and one just outside it (UTC 12th)."""
    fake.seed_event("primary", "Standup early", "2026-11-12T00:30:00+02:00", "2026-11-12T01:00:00+02:00")
    fake.seed_event("primary", "Standup late", "2026-11-13T00:30:00+02:00", "2026-11-13T01:00:00+02:00")


# ── Create / update ──────────────────────────────────────────────────────────


async def test_create_offset_less_time_is_read_in_the_calendar_zone(factory, fake):
    await google__create_event(factory, summary="Dentist", start="2026-11-12T15:00:00", end="2026-11-12T16:00:00")

    [event] = fake.events_on("primary")
    assert fake.start_of("primary", event["id"]) == datetime(2026, 11, 12, 15, 0, tzinfo=KYIV)
    assert event["start"] == {"dateTime": "2026-11-12T15:00:00+02:00", "timeZone": "Europe/Kyiv"}


async def test_update_to_offset_less_time_keeps_calendar_zone_and_event_count(factory, fake):
    await google__create_event(factory, summary="Dentist", start="2026-11-12T15:00:00", end="2026-11-12T16:00:00")
    [created] = fake.events_on("primary")

    await google__update_event(factory, event_id=created["id"], start="2026-11-12T17:00:00", end="2026-11-12T18:00:00")

    events = fake.events_on("primary")
    assert len(events) == 1
    assert events[0]["start"]["dateTime"] == "2026-11-12T17:00:00+02:00"
    assert events[0]["end"]["dateTime"] == "2026-11-12T18:00:00+02:00"


async def test_update_of_event_without_stored_zone_uses_the_calendar_zone(factory, fake):
    """An event carrying no ``timeZone`` of its own falls back to the resolver, not settings."""
    event_id = fake.seed_event("primary", "Imported", "2026-11-12T09:00:00+02:00", "2026-11-12T10:00:00+02:00")

    await google__update_event(factory, event_id=event_id, start="2026-11-12T17:00:00", end="2026-11-12T18:00:00")

    assert fake.start_of("primary", event_id) == datetime(2026, 11, 12, 17, 0, tzinfo=KYIV)


async def test_time_only_update_keeps_the_events_own_zone(factory, fake):
    """An event deliberately set in New York on a Kyiv calendar stays in New York."""
    event_id = fake.seed_event(
        "primary", "NY call", "2026-11-12T09:00:00", "2026-11-12T10:00:00", time_zone="America/New_York"
    )

    await google__update_event(factory, event_id=event_id, start="2026-11-12T11:00:00", end="2026-11-12T12:00:00")

    event = fake.event("primary", event_id)
    assert fake.start_of("primary", event_id) == datetime(2026, 11, 12, 11, 0, tzinfo=NEW_YORK)
    assert event["start"]["timeZone"] == "America/New_York"
    assert event["end"]["timeZone"] == "America/New_York"


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        # Kyiv leaves summer time at 04:00 local on 2026-10-25.
        ("2026-10-25T15:00:00", "2026-10-25T15:00:00+02:00"),
        ("2026-10-25T01:00:00", "2026-10-25T01:00:00+03:00"),
    ],
)
async def test_create_on_dst_change_date_lands_at_requested_wall_clock(factory, fake, start, expected):
    end = start.replace("T15:", "T16:").replace("T01:", "T02:")
    await google__create_event(factory, summary="DST day", start=start, end=end)

    [event] = fake.events_on("primary")
    assert event["start"]["dateTime"] == expected


async def test_secondary_calendar_uses_its_own_zone(factory, fake):
    await google__create_event(
        factory, summary="Board", start="2026-11-12T09:00:00", end="2026-11-12T10:00:00", calendar_id=WORK_CAL
    )

    [event] = fake.events_on(WORK_CAL)
    assert fake.start_of(WORK_CAL, event["id"]) == datetime(2026, 11, 12, 9, 0, tzinfo=NEW_YORK)
    assert event["start"]["timeZone"] == "America/New_York"
    assert fake.events_on("primary") == []


# ── Range bounds ─────────────────────────────────────────────────────────────


async def test_list_with_offset_less_bounds_covers_the_local_day(factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__list_events(factory, time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_list_with_date_only_bounds_covers_that_whole_local_day(factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__list_events(factory, time_min="2026-11-12", time_max="2026-11-12")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_search_with_offset_less_bounds_covers_the_local_day(factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__search_events(
        factory, query="Standup", time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00"
    )

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_search_without_bounds_sends_none(factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__search_events(factory, query="Standup")

    [sent] = fake.sent("events.list")
    assert "timeMin" not in sent
    assert "timeMax" not in sent
    assert "Found 2 event(s)" in result


async def test_freebusy_with_offset_less_bounds_covers_the_local_day(factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__check_freebusy(
        factory, emails="primary", time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00"
    )

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == (
        "2026-11-12T00:00:00+02:00",
        "2026-11-13T00:00:00+02:00",
    )
    assert "2026-11-11T22:30:00Z → 2026-11-11T23:00:00Z" in result
    assert "2026-11-12T22:30:00Z" not in result


# ── Offset-bearing input passes through ──────────────────────────────────────


async def test_bound_with_offset_or_z_is_sent_unchanged_next_to_a_resolved_one(factory, fake):
    await google__list_events(factory, time_min="2026-11-12T08:00:00Z", time_max="2026-11-13T00:00:00")

    [sent] = fake.sent("events.list")
    assert sent["timeMin"] == "2026-11-12T08:00:00Z"
    assert sent["timeMax"] == "2026-11-13T00:00:00+02:00"


async def test_freebusy_bounds_with_offsets_are_sent_unchanged(factory, fake):
    await google__check_freebusy(
        factory, emails="primary", time_min="2026-11-12T08:00:00Z", time_max="2026-11-12T20:00:00+01:00"
    )

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == ("2026-11-12T08:00:00Z", "2026-11-12T20:00:00+01:00")


async def test_create_with_offset_or_z_sends_times_unchanged_in_the_calendar_zone(factory, fake):
    await google__create_event(factory, summary="Offset", start="2026-11-12T10:00:00+05:00", end="2026-11-12T08:00:00Z")

    [insert] = fake.sent("events.insert")
    assert insert["body"]["start"] == {"dateTime": "2026-11-12T10:00:00+05:00", "timeZone": "Europe/Kyiv"}
    assert insert["body"]["end"] == {"dateTime": "2026-11-12T08:00:00Z", "timeZone": "Europe/Kyiv"}


# ── Resolver order ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("calendar_get_error", "user_timezone", "expected_zone", "expected_start"),
    [
        # Calendar zone wins over the identity zone and PINCER_TIMEZONE.
        (None, "America/New_York", "Europe/Kyiv", "2026-11-12T15:00:00+02:00"),
        # Calendar lookup fails (503) → identity zone, no error.
        (503, "America/New_York", "America/New_York", "2026-11-12T15:00:00-05:00"),
        # Calendar lookup fails (403) and no identity zone → PINCER_TIMEZONE.
        (403, "", "Europe/Berlin", "2026-11-12T15:00:00+01:00"),
        # An unknown identity zone is skipped, not fatal.
        (403, "Mars/Olympus_Mons", "Europe/Berlin", "2026-11-12T15:00:00+01:00"),
    ],
)
async def test_resolver_order_calendar_then_identity_then_settings(
    registry, fake, calendar_get_error, user_timezone, expected_zone, expected_start
):
    fake.calendar_get_error = calendar_get_error

    result = await registry.execute(
        "google__create_event",
        {"summary": "Order", "start": "2026-11-12T15:00:00", "end": "2026-11-12T16:00:00"},
        context={"user_id": "u1", "pincer_user_id": "u1", "timezone": user_timezone},
    )

    assert result.startswith("Event created")
    [insert] = fake.sent("events.insert")
    assert insert["body"]["start"] == {"dateTime": expected_start, "timeZone": expected_zone}


async def test_calendar_zone_is_cached_per_calendar_with_a_ttl(fake):
    from pincer.integrations.google.calendar_tz import CalendarTimezoneResolver

    now = [1000.0]
    resolver = CalendarTimezoneResolver(ttl_s=300, clock=lambda: now[0])

    assert await resolver.resolve(fake, "primary") == (KYIV, "calendar")
    assert await resolver.resolve(fake, "primary") == (KYIV, "calendar")
    assert len(fake.sent("calendars.get")) == 1

    assert await resolver.resolve(fake, WORK_CAL) == (NEW_YORK, "calendar")
    assert len(fake.sent("calendars.get")) == 2

    fake.calendar_data["primary"]["timeZone"] = "Asia/Tokyo"
    now[0] += 301
    assert await resolver.resolve(fake, "primary") == (ZoneInfo("Asia/Tokyo"), "calendar")
    assert len(fake.sent("calendars.get")) == 3


def test_tool_descriptions_say_offset_less_times_use_the_calendar_zone(registry):
    for name in ("google__create_event", "google__update_event", "google__list_events", "google__search_events"):
        schema = next(s for s in registry.get_schemas() if s["name"] == name)
        assert "calendar's timezone" in str(schema), name


# ── Voice appointment flow is unchanged ──────────────────────────────────────


async def test_voice_booking_sends_offset_aware_times_and_explicit_zone_unchanged(registry, fake):
    """The voice flow already sends offset-aware start/end plus an explicit zone: byte-identical body."""
    from pincer.voice import scheduling
    from pincer.voice.scheduling import AppointmentTask

    settings = SimpleNamespace(voice_timezone="Europe/Berlin", timezone="", scheduling_calendar_id="primary")
    task = AppointmentTask(
        task_id="task-1",
        user_id="u1",
        channel="telegram",
        target_number="+4930123456",
        contact_name="Dr. Müller",
        topic="Zahnreinigung",
        timeframe="next_week",
        duration_minutes=30,
        language="de",
        attendees="user@example.com",
        agreed_start="2026-08-25T14:00:00+02:00",
    )

    first = await scheduling._write_calendar_event(registry, settings, task, "CA_1")
    again = await scheduling._write_calendar_event(registry, settings, task, "CA_1")

    [insert] = fake.sent("events.insert")
    assert insert["body"]["start"] == {"dateTime": "2026-08-25T14:00:00+02:00", "timeZone": "Europe/Berlin"}
    assert insert["body"]["end"] == {"dateTime": "2026-08-25T14:30:00+02:00", "timeZone": "Europe/Berlin"}
    assert insert["body"]["extendedProperties"] == {"private": {"pincer_key": "pincer-appointment-task-1"}}
    assert insert["sendUpdates"] == "all"
    assert first == (
        "Event created: 'Zahnreinigung — Dr. Müller'\n"
        "Start: 2026-08-25T14:00:00+02:00 | End: 2026-08-25T14:30:00+02:00 (Europe/Berlin)\n"
        "ID: evt1\n"
        "Link: https://calendar.google.com/calendar/event?eid=evt1"
    )
    assert again.startswith(
        "Event already exists (idempotent): 'Zahnreinigung — Dr. Müller'\n"
        "Start: 2026-08-25T14:00:00+02:00 | End: 2026-08-25T14:30:00+02:00 (Europe/Berlin)\n"
        "ID: evt1\n"
    )
    assert len(fake.events_on("primary")) == 1
    assert fake.sent("calendars.get") == []


async def test_voice_freebusy_parser_reads_real_tool_output_unchanged(registry, fake):
    from pincer.voice.scheduling import parse_freebusy_output

    fake.seed_event("primary", "Busy", "2026-08-25T10:00:00+03:00", "2026-08-25T11:00:00+03:00")
    window_start = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    window_end = datetime(2026, 8, 25, 17, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    text = await registry.execute(
        "google__check_freebusy",
        {"emails": "primary", "time_min": window_start.isoformat(), "time_max": window_end.isoformat()},
    )

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == (
        "2026-08-25T09:00:00+02:00",
        "2026-08-25T17:00:00+02:00",
    )
    assert parse_freebusy_output(text) == [
        (datetime(2026, 8, 25, 7, 0, tzinfo=ZoneInfo("UTC")), datetime(2026, 8, 25, 8, 0, tzinfo=ZoneInfo("UTC")))
    ]
    assert fake.sent("calendars.get") == []
