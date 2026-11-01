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

from integrations.google.fake_calendar import FakeCalendarService
from pincer.integrations.google.tools_calendar import (
    google__check_freebusy,
    google__create_event,
    google__list_events,
    google__search_events,
    google__update_event,
)

KYIV = ZoneInfo("Europe/Kyiv")
NEW_YORK = ZoneInfo("America/New_York")
WORK_CAL = "work@group.calendar.google.com"


pytestmark = pytest.mark.usefixtures("berlin_deployment")


@pytest.fixture
def fake():
    return FakeCalendarService({"primary": "Europe/Kyiv", WORK_CAL: "America/New_York"})


def _seed_kyiv_day_edges(fake: FakeCalendarService) -> None:
    """One event just inside the local 12th (UTC 11th) and one just outside it (UTC 12th)."""
    fake.seed_event("primary", "Standup early", "2026-11-12T00:30:00+02:00", "2026-11-12T01:00:00+02:00")
    fake.seed_event("primary", "Standup late", "2026-11-13T00:30:00+02:00", "2026-11-13T01:00:00+02:00")


# ── Create / update ──────────────────────────────────────────────────────────


async def test_create_offset_less_time_is_read_in_the_calendar_zone(fake_factory, fake):
    await google__create_event(fake_factory, summary="Dentist", start="2026-11-12T15:00:00", end="2026-11-12T16:00:00")

    [event] = fake.events_on("primary")
    assert fake.start_of("primary", event["id"]) == datetime(2026, 11, 12, 15, 0, tzinfo=KYIV)
    assert event["start"] == {"dateTime": "2026-11-12T15:00:00+02:00", "timeZone": "Europe/Kyiv"}


async def test_update_to_offset_less_time_keeps_calendar_zone_and_event_count(fake_factory, fake):
    await google__create_event(fake_factory, summary="Dentist", start="2026-11-12T15:00:00", end="2026-11-12T16:00:00")
    [created] = fake.events_on("primary")

    await google__update_event(
        fake_factory, event_id=created["id"], start="2026-11-12T17:00:00", end="2026-11-12T18:00:00"
    )

    events = fake.events_on("primary")
    assert len(events) == 1
    assert events[0]["start"]["dateTime"] == "2026-11-12T17:00:00+02:00"
    assert events[0]["end"]["dateTime"] == "2026-11-12T18:00:00+02:00"


async def test_update_of_event_without_stored_zone_uses_the_calendar_zone(fake_factory, fake):
    """An event carrying no ``timeZone`` of its own falls back to the resolver, not settings."""
    event_id = fake.seed_event("primary", "Imported", "2026-11-12T09:00:00+02:00", "2026-11-12T10:00:00+02:00")

    await google__update_event(fake_factory, event_id=event_id, start="2026-11-12T17:00:00", end="2026-11-12T18:00:00")

    assert fake.start_of("primary", event_id) == datetime(2026, 11, 12, 17, 0, tzinfo=KYIV)


async def test_time_only_update_keeps_the_events_own_zone(fake_factory, fake):
    """An event deliberately set in New York on a Kyiv calendar stays in New York."""
    event_id = fake.seed_event(
        "primary", "NY call", "2026-11-12T09:00:00", "2026-11-12T10:00:00", time_zone="America/New_York"
    )

    await google__update_event(fake_factory, event_id=event_id, start="2026-11-12T11:00:00", end="2026-11-12T12:00:00")

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
async def test_create_on_dst_change_date_lands_at_requested_wall_clock(fake_factory, fake, start, expected):
    end = start.replace("T15:", "T16:").replace("T01:", "T02:")
    await google__create_event(fake_factory, summary="DST day", start=start, end=end)

    [event] = fake.events_on("primary")
    assert event["start"]["dateTime"] == expected


async def test_secondary_calendar_uses_its_own_zone(fake_factory, fake):
    await google__create_event(
        fake_factory, summary="Board", start="2026-11-12T09:00:00", end="2026-11-12T10:00:00", calendar_id=WORK_CAL
    )

    [event] = fake.events_on(WORK_CAL)
    assert fake.start_of(WORK_CAL, event["id"]) == datetime(2026, 11, 12, 9, 0, tzinfo=NEW_YORK)
    assert event["start"]["timeZone"] == "America/New_York"
    assert fake.events_on("primary") == []


# ── Range bounds ─────────────────────────────────────────────────────────────


async def test_list_with_offset_less_bounds_covers_the_local_day(fake_factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__list_events(fake_factory, time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_list_with_date_only_bounds_covers_that_whole_local_day(fake_factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-12")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_search_with_offset_less_bounds_covers_the_local_day(fake_factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__search_events(
        fake_factory, query="Standup", time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00"
    )

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2026-11-12T00:00:00+02:00", "2026-11-13T00:00:00+02:00")
    assert "Standup early" in result
    assert "Standup late" not in result


async def test_search_without_bounds_sends_none(fake_factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__search_events(fake_factory, query="Standup")

    [sent] = fake.sent("events.list")
    assert "timeMin" not in sent
    assert "timeMax" not in sent
    assert "Found 2 event(s)" in result


async def test_freebusy_with_offset_less_bounds_covers_the_local_day(fake_factory, fake):
    _seed_kyiv_day_edges(fake)

    result = await google__check_freebusy(
        fake_factory, emails="primary", time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00"
    )

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == (
        "2026-11-12T00:00:00+02:00",
        "2026-11-13T00:00:00+02:00",
    )
    assert "2026-11-11T22:30:00Z → 2026-11-11T23:00:00Z" in result
    assert "2026-11-12T22:30:00Z" not in result


# ── Offset-bearing input passes through ──────────────────────────────────────


async def test_bound_with_offset_or_z_is_sent_normalised_next_to_a_resolved_one(fake_factory, fake):
    await google__list_events(fake_factory, time_min="2026-11-12T08:00:00Z", time_max="2026-11-13T00:00:00")

    [sent] = fake.sent("events.list")
    assert sent["timeMin"] == "2026-11-12T08:00:00+00:00"
    assert sent["timeMax"] == "2026-11-13T00:00:00+02:00"


async def test_freebusy_bounds_with_offsets_keep_their_instant_in_rfc3339(fake_factory, fake):
    await google__check_freebusy(
        fake_factory, emails="primary", time_min="2026-11-12T08:00:00Z", time_max="2026-11-12T20:00:00+01:00"
    )

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == (
        "2026-11-12T08:00:00+00:00",
        "2026-11-12T20:00:00+01:00",
    )


async def test_create_with_offset_or_z_keeps_the_instant_in_the_calendar_zone(fake_factory, fake):
    await google__create_event(
        fake_factory, summary="Offset", start="2026-11-12T10:00:00+05:00", end="2026-11-12T08:00:00Z"
    )

    [insert] = fake.sent("events.insert")
    assert insert["body"]["start"] == {"dateTime": "2026-11-12T10:00:00+05:00", "timeZone": "Europe/Kyiv"}
    assert insert["body"]["end"] == {"dateTime": "2026-11-12T08:00:00+00:00", "timeZone": "Europe/Kyiv"}


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
    fake_registry, fake, calendar_get_error, user_timezone, expected_zone, expected_start
):
    fake.calendar_get_error = calendar_get_error

    result = await fake_registry.execute(
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


def test_tool_descriptions_say_offset_less_times_use_the_calendar_zone(fake_registry):
    for name in ("google__create_event", "google__update_event", "google__list_events", "google__search_events"):
        schema = next(s for s in fake_registry.get_schemas() if s["name"] == name)
        assert "calendar's timezone" in str(schema), name


# ── Voice appointment flow is unchanged ──────────────────────────────────────


async def test_voice_booking_sends_offset_aware_times_and_explicit_zone_unchanged(fake_registry, fake):
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

    first = await scheduling._write_calendar_event(fake_registry, settings, task, "CA_1")
    again = await scheduling._write_calendar_event(fake_registry, settings, task, "CA_1")

    first_insert, second_insert = fake.sent("events.insert")
    assert first_insert["body"]["start"] == {"dateTime": "2026-08-25T14:00:00+02:00", "timeZone": "Europe/Berlin"}
    assert first_insert["body"]["end"] == {"dateTime": "2026-08-25T14:30:00+02:00", "timeZone": "Europe/Berlin"}
    assert first_insert["body"]["extendedProperties"] == {"private": {"pincer_key": "pincer-appointment-task-1"}}
    assert first_insert["sendUpdates"] == "all"
    assert second_insert["body"]["id"] == first_insert["body"]["id"]
    event_id = first_insert["body"]["id"]
    assert first == (
        "Event created: 'Zahnreinigung — Dr. Müller'\n"
        "Start: 2026-08-25T14:00:00+02:00 | End: 2026-08-25T14:30:00+02:00 (Europe/Berlin)\n"
        f"ID: {event_id}\n"
        f"Link: https://calendar.google.com/calendar/event?eid={event_id}"
    )
    assert again.startswith(
        "Event already created: 'Zahnreinigung — Dr. Müller' (it already exists — not duplicated)\n"
        "Start: 2026-08-25T14:00:00+02:00 | End: 2026-08-25T14:30:00+02:00 (Europe/Berlin)\n"
        f"ID: {event_id}\n"
    )
    assert len(fake.events_on("primary")) == 1
    assert fake.sent("calendars.get") == []


async def test_voice_freebusy_parser_reads_real_tool_output_unchanged(fake_registry, fake):
    from pincer.voice.scheduling import parse_freebusy_output

    fake.seed_event("primary", "Busy", "2026-08-25T10:00:00+03:00", "2026-08-25T11:00:00+03:00")
    window_start = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    window_end = datetime(2026, 8, 25, 17, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    text = await fake_registry.execute(
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


# ── Default range end (review R1) ────────────────────────────────────────────
# Dates are in 2027 so the old "now + 7 days" default can never reach them by
# accident; live on 2026-11-01, list_events(time_min="2026-11-12") answered
# "No events between 2026-11-12 and 2026-11-08" (an inverted range, no 400).


async def test_list_with_only_a_date_lists_that_whole_local_day(fake_factory, fake):
    fake.seed_event("primary", "Repro B", "2027-03-15T15:00:00+02:00", "2027-03-15T16:00:00+02:00")
    fake.seed_event("primary", "Next day", "2027-03-16T09:00:00+02:00", "2027-03-16T10:00:00+02:00")

    result = await google__list_events(fake_factory, time_min="2027-03-15")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2027-03-15T00:00:00+02:00", "2027-03-16T00:00:00+02:00")
    assert "Repro B" in result
    assert "Next day" not in result


async def test_list_with_only_a_start_time_covers_seven_days_from_it(fake_factory, fake):
    fake.seed_event("primary", "Day six", "2027-03-21T09:00:00+02:00", "2027-03-21T10:00:00+02:00")
    fake.seed_event("primary", "Day eight", "2027-03-23T11:00:00+02:00", "2027-03-23T12:00:00+02:00")

    result = await google__list_events(fake_factory, time_min="2027-03-15T10:00:00")

    [sent] = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == ("2027-03-15T10:00:00+02:00", "2027-03-22T10:00:00+02:00")
    assert "Day six" in result
    assert "Day eight" not in result


async def test_freebusy_with_only_a_date_covers_that_whole_local_day(fake_factory, fake):
    fake.seed_event("primary", "Repro B", "2027-03-15T15:00:00+02:00", "2027-03-15T16:00:00+02:00")

    result = await google__check_freebusy(fake_factory, emails="primary", time_min="2027-03-15")

    [sent] = fake.sent("freebusy.query")
    assert (sent["body"]["timeMin"], sent["body"]["timeMax"]) == (
        "2027-03-15T00:00:00+02:00",
        "2027-03-16T00:00:00+02:00",
    )
    assert "2027-03-15T13:00:00Z → 2027-03-15T14:00:00Z" in result


async def test_fake_returns_nothing_for_an_inverted_range_like_google(fake):
    fake.seed_event("primary", "Repro B", "2026-11-12T15:00:00+02:00", "2026-11-12T16:00:00+02:00")

    result = (
        fake.events()
        .list(calendarId="primary", timeMin="2026-11-12T00:00:00+02:00", timeMax="2026-11-08T00:00:00Z")
        .execute()
    )

    assert result["items"] == []


def test_legacy_kiev_name_resolves_without_a_system_zone_database():
    """Google reports the owner's calendar as Europe/Kiev; python:3.14-slim (Debian 13)
    has no such system file, so zoneinfo must fall back to the PyPI tzdata package
    (an unconditional dependency), which carries the backward links."""
    import zoneinfo

    saved = zoneinfo.TZPATH
    zoneinfo.reset_tzpath([])
    try:
        zone = zoneinfo.ZoneInfo.no_cache("Europe/Kiev")
    finally:
        zoneinfo.reset_tzpath(saved)

    assert zone.key == "Europe/Kiev"
    assert (
        datetime(2026, 11, 12, 12, 0, tzinfo=zone).utcoffset() == datetime(2026, 11, 12, 12, 0, tzinfo=KYIV).utcoffset()
    )


# ── Resolver hardening (review R2, R4, R10, R12) ─────────────────────────────


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("2026-11-12T10:00:00+0200", "2026-11-12T10:00:00+02:00"),
        ("2026-11-12T10:00+02", "2026-11-12T10:00:00+02:00"),
        ("2026-11-12T10:00+02:00", "2026-11-12T10:00:00+02:00"),
    ],
)
async def test_offset_bearing_values_are_sent_as_rfc3339(fake_factory, fake, given, sent):
    await google__list_events(fake_factory, time_min=given, time_max="2026-11-13T00:00:00+02:00")
    await google__create_event(fake_factory, summary="Norm", start=given, end="2026-11-12T11:00:00+02:00")

    assert fake.sent("events.list")[0]["timeMin"] == sent
    assert fake.sent("events.insert")[0]["body"]["start"]["dateTime"] == sent


async def test_stale_calendar_zone_is_kept_when_a_refetch_fails(fake):
    from pincer.integrations.google.calendar_tz import CalendarTimezoneResolver

    now = [1000.0]
    resolver = CalendarTimezoneResolver(ttl_s=300, clock=lambda: now[0])
    assert await resolver.resolve(fake, "primary") == (KYIV, "calendar")

    now[0] += 301
    fake.calendar_get_error = 503
    zone, source = await resolver.resolve(fake, "primary")

    assert (zone.key, source) == ("Europe/Kyiv", "calendar")
    assert len(fake.sent("calendars.get")) == 2


async def test_failed_calendar_zone_lookup_is_not_retried_for_a_minute(fake):
    from pincer.integrations.google.calendar_tz import CalendarTimezoneResolver

    now = [1000.0]
    resolver = CalendarTimezoneResolver(ttl_s=300, clock=lambda: now[0])
    fake.calendar_get_error = 503

    assert (await resolver.resolve(fake, "primary"))[1] == "settings"
    now[0] += 59
    assert (await resolver.resolve(fake, "primary"))[1] == "settings"
    assert len(fake.sent("calendars.get")) == 1

    fake.calendar_get_error = None
    now[0] += 2
    assert await resolver.resolve(fake, "primary") == (KYIV, "calendar")
    assert len(fake.sent("calendars.get")) == 2


async def test_failed_calendar_zone_lookup_logs_no_error(fake, caplog):
    from pincer.integrations.google.calendar_tz import CalendarTimezoneResolver

    fake.calendar_get_error = 503
    with caplog.at_level("DEBUG"):
        await CalendarTimezoneResolver().resolve(fake, "primary")

    assert [r for r in caplog.records if r.levelname == "ERROR"] == []
    assert any("Could not read the timezone of calendar" in r.getMessage() for r in caplog.records)


def test_clock_zone_warns_when_settings_cannot_load(monkeypatch, caplog):
    from pincer.integrations.google.calendar_tz import clock_zone

    def _broken():
        raise RuntimeError("settings exploded")

    monkeypatch.setattr("pincer.config.get_settings_relaxed", _broken)
    with caplog.at_level("WARNING"):
        zone = clock_zone("")

    assert zone.key == "Europe/Berlin"
    assert any(r.levelname == "WARNING" and "settings exploded" in r.getMessage() for r in caplog.records)


# ── Fake fidelity and DST gap (review R6, R7) ────────────────────────────────


def test_fake_rejects_a_bare_date_in_datetime_like_google(fake):
    from googleapiclient.errors import HttpError

    body = {
        "summary": "Bad",
        "start": {"dateTime": "2026-11-12", "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": "2026-11-13", "timeZone": "Europe/Kyiv"},
    }
    with pytest.raises(HttpError) as raised:
        fake.events().insert(calendarId="primary", body=body).execute()

    assert raised.value.status_code == 400
    assert fake.events_on("primary") == []


def test_fake_all_day_event_spans_the_calendars_local_day(fake):
    fake.seed_all_day("primary", "Holiday", "2026-11-12", "2026-11-13")

    early = (
        fake.events()
        .list(calendarId="primary", timeMin="2026-11-12T00:00:00+02:00", timeMax="2026-11-12T01:00:00+02:00")
        .execute()
    )
    after = (
        fake.events()
        .list(calendarId="primary", timeMin="2026-11-13T00:30:00+02:00", timeMax="2026-11-13T01:30:00+02:00")
        .execute()
    )

    assert [e["summary"] for e in early["items"]] == ["Holiday"]
    assert after["items"] == []


async def test_dst_gap_time_shifts_forward_by_the_gap(fake_factory, fake):
    """03:30 does not exist in Kyiv on 2026-03-29 (03:00 → 04:00). It takes the offset in
    force before the gap (+02:00), i.e. lands at 04:30 local — pinned, documented in calendar_tz."""
    await google__create_event(fake_factory, summary="Gap", start="2026-03-29T03:30:00", end="2026-03-29T05:00:00")

    [insert] = fake.sent("events.insert")
    assert insert["body"]["start"]["dateTime"] == "2026-03-29T03:30:00+02:00"
    assert fake.events_on("primary")[0]["start"]["dateTime"] == "2026-03-29T04:30:00+03:00"
