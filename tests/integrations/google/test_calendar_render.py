"""
Pincer reports local, stored times (calendar reliability, slice B).

Driven through the real calendar tools against the stateful fake. The owner's
calendar is reported by Google as ``Europe/Kiev`` (legacy alias) while the
agent clock is ``Europe/Berlin`` (PINCER_TIMEZONE) or ``Europe/Kyiv`` (identity).
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from integrations.google.fake_calendar import FakeCalendarService
from pincer.integrations.google.calendar_render import EDIT_HINT
from pincer.integrations.google.tools_calendar import (
    google__create_event,
    google__get_event,
    google__list_events,
    google__search_events,
    google__update_event,
)
from pincer.llm.base import ToolCall

KIEV_NOTE = (
    "Note: times are in this calendar's timezone (Europe/Kiev), which differs from your clock timezone (Europe/Berlin)."
)
LINK1 = "https://calendar.google.com/calendar/event?eid=evt1"
PATCH_TOOLS = {"google__accept_event", "google__decline_event", "google__add_google_meet"}


pytestmark = pytest.mark.usefixtures("berlin_deployment")


@pytest.fixture
def fake():
    return FakeCalendarService({"primary": "Europe/Kiev", "berlin@group.calendar.google.com": "Europe/Berlin"})


def _created(fake: FakeCalendarService) -> tuple[str, str]:
    """ID and link of the event the last insert created (its client-supplied ID)."""
    event_id = fake.sent("events.insert")[-1]["body"]["id"]
    return event_id, f"https://calendar.google.com/calendar/event?eid={event_id}"


def _seed_day(fake: FakeCalendarService) -> None:
    """A New-York-zoned standup (03:00 NY = 10:00 Kyiv) and an all-day holiday on 2026-11-12."""
    fake.seed_event("primary", "Standup", "2026-11-12T03:00:00", "2026-11-12T04:00:00", time_zone="America/New_York")
    fake.seed_all_day("primary", "Holiday", "2026-11-12", "2026-11-13")


# ── Reads ────────────────────────────────────────────────────────────────────


async def test_list_shows_local_times_zone_header_all_day_ids_and_note(fake_factory, fake):
    _seed_day(fake)

    result = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-12")

    assert result == (
        "2 event(s) (2026-11-12), times in Europe/Kiev:\n"
        f"{KIEV_NOTE}\n"
        "  2026-11-12 → 2026-11-12 — Holiday | all day\n"
        "  ID: evt2\n"
        "  2026-11-12T10:00:00+02:00 → 2026-11-12T11:00:00+02:00 — Standup | event zone America/New_York\n"
        "  ID: evt1\n"
        f"{EDIT_HINT}"
    )


async def test_search_shows_local_times_and_zone_header(fake_factory, fake):
    _seed_day(fake)

    result = await google__search_events(fake_factory, query="Standup")

    assert result.splitlines()[0] == "Found 1 event(s), times in Europe/Kiev:"
    assert (
        "  2026-11-12T10:00:00+02:00 → 2026-11-12T11:00:00+02:00 — Standup | event zone America/New_York"
        in result.splitlines()
    )


async def test_get_shows_local_time_and_zone(fake_factory, fake):
    _seed_day(fake)

    result = await google__get_event(fake_factory, event_id="evt1")

    lines = result.splitlines()
    assert "Start:       2026-11-12T10:00:00+02:00" in lines
    assert "End:         2026-11-12T11:00:00+02:00" in lines
    assert "Times shown in: Europe/Kiev" in lines
    assert KIEV_NOTE in lines


async def test_get_all_day_event_says_all_day(fake_factory, fake):
    _seed_day(fake)

    result = await google__get_event(fake_factory, event_id="evt2")

    assert "Start:       2026-11-12 (all day)" in result.splitlines()
    assert "End:         2026-11-12 (all day)" in result.splitlines()


async def test_multi_day_all_day_event_shows_its_last_day_not_googles_exclusive_end(fake_factory, fake):
    fake.seed_all_day("primary", "Trip", "2026-11-12", "2026-11-15")

    result = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-14")

    assert "  2026-11-12 → 2026-11-14 — Trip | all day" in result.splitlines()


async def test_every_listed_event_carries_its_id_line(fake_factory, fake):
    _seed_day(fake)
    fake.seed_event("primary", "Lunch", "2026-11-12T13:00:00+02:00", "2026-11-12T14:00:00+02:00")

    result = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-12")

    ids = [line.strip() for line in result.splitlines() if line.startswith("  ID: ")]
    assert ids == ["ID: evt2", "ID: evt1", "ID: evt3"]


# ── Mismatch note ────────────────────────────────────────────────────────────


async def test_kiev_calendar_under_kyiv_identity_clock_shows_no_note(fake_registry, fake):
    """Europe/Kiev (Google's legacy alias) and Europe/Kyiv are one zone."""
    _seed_day(fake)

    result = await fake_registry.execute(
        "google__list_events",
        {"time_min": "2026-11-12", "time_max": "2026-11-12"},
        context={"timezone": "Europe/Kyiv"},
    )

    assert result.splitlines()[0] == "2 event(s) (2026-11-12), times in Europe/Kiev:"
    assert "Note:" not in result


async def test_matching_zones_show_no_note(fake_factory, fake):
    fake.seed_event("berlin@group.calendar.google.com", "Gym", "2026-11-12T18:00:00+01:00", "2026-11-12T19:00:00+01:00")

    result = await google__list_events(
        fake_factory, calendar_id="berlin@group.calendar.google.com", time_min="2026-11-12", time_max="2026-11-12"
    )

    assert result.splitlines()[0] == "1 event(s) (2026-11-12), times in Europe/Berlin:"
    assert "Note:" not in result


# ── Writes ───────────────────────────────────────────────────────────────────


async def test_create_confirmation_quotes_stored_times_zone_id_and_link(fake_factory, fake):
    result = await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    event_id, link = _created(fake)
    assert result == (
        "Event created: 'Dentist'\n"
        "Start: 2026-11-12T10:00:00+02:00 | End: 2026-11-12T11:00:00+02:00 (Europe/Kiev)\n"
        f"{KIEV_NOTE}\n"
        f"ID: {event_id}\n"
        f"Link: {link}"
    )


async def test_create_reports_what_google_stored_not_what_was_requested(fake_factory, fake):
    def _google_shifts_it(event):
        for key in ("start", "end"):
            moved = (
                FakeCalendarService.instants(event, "Europe/Kiev")[0 if key == "start" else 1] + timedelta(minutes=30)
            ).isoformat()
            event[key]["dateTime"] = moved

    fake.on_store = _google_shifts_it

    result = await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    assert "Start: 2026-11-12T10:30:00+02:00 | End: 2026-11-12T11:30:00+02:00 (Europe/Kiev)" in result.splitlines()


async def test_update_confirmation_quotes_stored_times_zone_id_and_link(fake_factory, fake):
    await google__create_event(fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00")
    event_id, link = _created(fake)

    result = await google__update_event(
        fake_factory, event_id=event_id, start="2026-11-12T17:00:00", end="2026-11-12T18:00:00"
    )

    assert result == (
        "Event updated: 'Dentist'\n"
        "Start: 2026-11-12T17:00:00+02:00 | End: 2026-11-12T18:00:00+02:00 (Europe/Kiev)\n"
        f"{KIEV_NOTE}\n"
        f"ID: {event_id}\n"
        f"Link: {link}"
    )


async def test_create_then_get_by_returned_id_returns_the_same_event(fake_factory, fake):
    created = await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )
    event_id = re.search(r"^ID: (\S+)$", created, re.MULTILINE).group(1)
    created_start = re.search(r"^Start: (\S+) \|", created, re.MULTILINE).group(1)

    fetched = await google__get_event(fake_factory, event_id=event_id)

    assert "Title:       Dentist" in fetched.splitlines()
    assert f"Start:       {created_start}" in fetched.splitlines()
    assert f"ID:          {event_id}" in fetched.splitlines()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("google__create_event", {"summary": "Dentist", "start": "2026-11-12T10:00:00", "end": "2026-11-12T11:00:00"}),
        ("google__update_event", {"event_id": "evt1", "summary": "Dentist (moved)", "start": "2026-11-12T17:00:00"}),
        ("google__accept_event", {"event_id": "evt1"}),
        ("google__decline_event", {"event_id": "evt1"}),
        ("google__add_google_meet", {"event_id": "evt1"}),
        ("google__move_event", {"event_id": "evt1", "destination_calendar_id": "berlin@group.calendar.google.com"}),
    ],
)
async def test_write_response_without_id_is_an_unconfirmed_error(
    fake_registry, fake, settings, mock_llm, session_manager, cost_tracker, tool, arguments
):
    from pincer.core.agent import Agent

    fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00+02:00", "2026-11-12T11:00:00+02:00")
    fake.drop_id_on_write = True
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, fake_registry)

    result = await agent._execute_tool(ToolCall(id="tc1", name=tool, arguments=dict(arguments)), "u1", "test")

    assert result.is_error is True
    assert "not confirmed" in result.content
    assert "may or may not exist" in result.content
    assert "created" not in result.content.lower()
    assert "updated" not in result.content.lower()
    assert not result.content.startswith(("Accepted", "Declined", "Google Meet added", "Event evt1 moved"))
    if tool in PATCH_TOOLS:  # precondition: the write really reached Google
        assert [name for name, _kw in fake.calls if name == "events.patch"] == ["events.patch"]
    if tool == "google__move_event":  # precondition: the move really committed
        assert [e["id"] for e in fake.events_on("berlin@group.calendar.google.com")] == ["evt1"]


# ── Voice format contract on real renderer output ────────────────────────────


async def test_voice_link_extraction_reads_real_create_output(fake_registry, fake):
    result = await fake_registry.execute(
        "google__create_event",
        {
            "summary": "Zahnreinigung",
            "start": "2026-08-25T14:00:00+02:00",
            "end": "2026-08-25T14:30:00+02:00",
            "timezone": "Europe/Berlin",
        },
    )

    # The exact patterns voice/scheduling.py and voice/receptionist/session.py use.
    _event_id, link = _created(fake)
    assert re.search(r"Link: (\S+)", result).group(1) == link
    assert re.search(r"Link:\s*(\S+)", result).group(1) == link


async def test_voice_tool_speech_reads_real_list_output(fake_registry, fake):
    from pincer.voice import tool_speech

    fake.seed_event("primary", "Beratung", "2026-11-12T14:00:00+02:00", "2026-11-12T15:00:00+02:00")
    fake.seed_event("primary", "Termin", "2026-11-12T16:00:00+02:00", "2026-11-12T17:00:00+02:00")
    result = await fake_registry.execute("google__list_events", {"time_min": "2026-11-12", "time_max": "2026-11-12"})

    spoken = tool_speech.render("google__list_events", result, "en")

    assert spoken.startswith("There are 2 appointment(s): ")
    assert "Beratung" in spoken
    assert "Termin" in spoken


# ── Live-measured Google behaviour and review fixes ──────────────────────────


async def test_fake_stores_datetime_in_the_calendar_offset_keeping_the_events_zone(fake_factory, fake):
    """Measured on the owner's real calendar (2026-11-01): an event created on the Europe/Kiev
    calendar with timeZone America/New_York at 10:00 came back from events().get as
    start={'dateTime': '2026-11-21T17:00:00+02:00', 'timeZone': 'America/New_York'} — the
    calendar's offset, the event's zone. The fake must do the same."""
    await google__create_event(
        fake_factory,
        summary="NY call",
        start="2026-11-21T10:00:00",
        end="2026-11-21T11:00:00",
        timezone="America/New_York",
    )

    assert fake.event("primary", _created(fake)[0])["start"] == {
        "dateTime": "2026-11-21T17:00:00+02:00",
        "timeZone": "America/New_York",
    }


async def test_caller_zone_confirmation_names_the_events_zone_not_the_calendars(fake_registry, fake):
    result = await fake_registry.execute(
        "google__create_event",
        {
            "summary": "NY call",
            "start": "2026-11-21T10:00:00",
            "end": "2026-11-21T11:00:00",
            "timezone": "America/New_York",
        },
        context={"timezone": "Europe/Kyiv"},
    )

    assert result == (
        "Event created: 'NY call'\n"
        "Start: 2026-11-21T10:00:00-05:00 | End: 2026-11-21T11:00:00-05:00 (America/New_York)\n"
        "Note: times are in the event's timezone (America/New_York), which differs from "
        "your clock timezone (Europe/Kyiv).\n"
        f"ID: {_created(fake)[0]}\n"
        f"Link: {_created(fake)[1]}"
    )


async def test_voice_style_booking_note_names_the_events_zone_without_a_calendar_lookup(fake_registry, fake):
    result = await fake_registry.execute(
        "google__create_event",
        {
            "summary": "Zahnreinigung",
            "start": "2026-08-25T14:00:00+02:00",
            "end": "2026-08-25T14:30:00+02:00",
            "timezone": "Europe/Berlin",
        },
        context={"timezone": "Europe/Kyiv"},
    )

    assert (
        "Note: times are in the event's timezone (Europe/Berlin), which differs from your clock timezone (Europe/Kyiv)."
    ) in result.splitlines()
    assert "this calendar's timezone" not in result
    assert fake.sent("calendars.get") == []


async def test_list_header_shows_the_inclusive_last_local_day(fake_factory, fake):
    fake.seed_event("primary", "Lunch", "2026-11-13T13:00:00+02:00", "2026-11-13T14:00:00+02:00")

    two_days = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-13")
    midday_end = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-13T15:00:00")
    empty_day = await google__list_events(fake_factory, time_min="2026-11-20", time_max="2026-11-20")

    assert two_days.splitlines()[0] == "1 event(s) (2026-11-12 – 2026-11-13), times in Europe/Kiev:"
    assert midday_end.splitlines()[0] == "1 event(s) (2026-11-12 – 2026-11-13), times in Europe/Kiev:"
    assert empty_day == "No events on 2026-11-20."


async def test_get_shows_the_events_own_zone_when_it_differs_from_the_calendars(fake_factory, fake):
    _seed_day(fake)  # evt1 is America/New_York on the Europe/Kiev calendar
    same = fake.seed_event("primary", "Local", "2026-11-12T12:00:00", "2026-11-12T13:00:00", time_zone="Europe/Kyiv")

    differs = await google__get_event(fake_factory, event_id="evt1")
    equivalent = await google__get_event(fake_factory, event_id=same)

    assert "Times shown in: Europe/Kiev" in differs.splitlines()
    assert "Event's own timezone: America/New_York" in differs.splitlines()
    assert "own timezone" not in equivalent


# ── Confirmation zone comes from the stored copy (B review R1) ───────────────


async def test_update_confirmation_zone_is_the_stored_one_not_the_requested_one(fake_factory, fake):
    """update(summary only, timezone="America/New_York") leaves Google storing Europe/Kiev."""
    fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00", "2026-11-12T11:00:00", time_zone="Europe/Kiev")

    result = await google__update_event(fake_factory, event_id="evt1", summary="Renamed", timezone="America/New_York")

    assert fake.event("primary", "evt1")["start"]["timeZone"] == "Europe/Kiev"
    assert result == (
        "Event updated: 'Renamed'\n"
        "Start: 2026-11-12T10:00:00+02:00 | End: 2026-11-12T11:00:00+02:00 (Europe/Kiev)\n"
        f"{KIEV_NOTE}\n"
        "ID: evt1\n"
        f"Link: {LINK1}"
    )


async def test_idempotent_hit_shows_the_existing_events_stored_zone(fake_factory, fake):
    await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00", idempotency_key="k1"
    )

    # The same instant, asked for in New York time: same key + same details = the same event.
    again = await google__create_event(
        fake_factory,
        summary="Dentist",
        start="2026-11-12T03:00:00",
        end="2026-11-12T04:00:00",
        timezone="America/New_York",
        idempotency_key="k1",
    )

    assert len(fake.events_on("primary")) == 1
    assert again.splitlines()[:2] == [
        "Event already created: 'Dentist' (it already exists — not duplicated)",
        "Start: 2026-11-12T10:00:00+02:00 | End: 2026-11-12T11:00:00+02:00 (Europe/Kiev)",
    ]
    assert "event's timezone" not in again


# ── All-day only, missing stored times (B review R3, R4) ─────────────────────


async def test_all_day_only_results_show_no_mismatch_note(fake_factory, fake):
    fake.seed_all_day("primary", "Holiday", "2026-11-12", "2026-11-13")

    listed = await google__list_events(fake_factory, time_min="2026-11-12", time_max="2026-11-12")
    got = await google__get_event(fake_factory, event_id="evt1")

    assert listed.splitlines()[0] == "1 event(s) (2026-11-12), times in Europe/Kiev:"
    assert "Note:" not in listed
    assert "Note:" not in got


async def test_write_response_with_id_but_no_times_says_stored_times_unavailable(fake_factory, fake):
    def _response_without_times(event):
        event.pop("start", None)
        event.pop("end", None)

    fake.on_store = _response_without_times

    result = await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    event_id, link = _created(fake)
    assert result == (f"Event created: 'Dentist'\nStart: (stored times unavailable)\nID: {event_id}\nLink: {link}")


# ── Moving an event keeps its duration (pre-existing bug, in scope) ──────────


async def test_moving_only_the_start_keeps_the_duration(fake_factory, fake):
    fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00+02:00", "2026-11-12T11:00:00+02:00")

    result = await google__update_event(fake_factory, event_id="evt1", start="2026-11-12T17:00:00")

    [event] = fake.events_on("primary")
    assert (event["start"]["dateTime"], event["end"]["dateTime"]) == (
        "2026-11-12T17:00:00+02:00",
        "2026-11-12T18:00:00+02:00",
    )
    assert "Start: 2026-11-12T17:00:00+02:00 | End: 2026-11-12T18:00:00+02:00 (Europe/Kiev)" in result.splitlines()


async def test_moving_only_the_start_of_an_all_day_event_keeps_its_days(fake_factory, fake):
    fake.seed_all_day("primary", "Trip", "2026-11-20", "2026-11-23")

    await google__update_event(fake_factory, event_id="evt1", start="2026-11-25")

    [event] = fake.events_on("primary")
    assert (event["start"], event["end"]) == ({"date": "2026-11-25"}, {"date": "2026-11-28"})


async def test_changing_only_the_end_keeps_the_start(fake_factory, fake):
    fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00+02:00", "2026-11-12T11:00:00+02:00")

    await google__update_event(fake_factory, event_id="evt1", end="2026-11-12T12:00:00")

    [event] = fake.events_on("primary")
    assert (event["start"]["dateTime"], event["end"]["dateTime"]) == (
        "2026-11-12T10:00:00+02:00",
        "2026-11-12T12:00:00+02:00",
    )


async def test_moving_between_all_day_and_timed_needs_an_end(fake_factory, fake):
    fake.seed_all_day("primary", "Trip", "2026-11-20", "2026-11-21")

    with pytest.raises(ValueError, match="needs both a new start and a new end"):
        await google__update_event(fake_factory, event_id="evt1", start="2026-11-25T10:00:00")

    assert fake.events_on("primary")[0]["start"] == {"date": "2026-11-20"}


# ── Update review fixes (FR1–FR5) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seed_all_day", "changes"),
    [
        (False, {"end": "2026-11-07"}),  # timed event, bare-date end
        (True, {"end": "2026-11-21T12:00:00"}),  # all-day event, timed end
        (False, {"start": "2026-11-13", "end": "2026-11-13T12:00:00"}),  # date start, timed end
    ],
)
async def test_mixed_all_day_and_timed_start_end_is_refused_before_writing(fake_factory, fake, seed_all_day, changes):
    if seed_all_day:
        fake.seed_all_day("primary", "Trip", "2026-11-20", "2026-11-21")
    else:
        fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00+02:00", "2026-11-12T11:00:00+02:00")

    with pytest.raises(ValueError, match="needs both a new start and a new end"):
        await google__update_event(fake_factory, event_id="evt1", **changes)

    assert fake.sent("events.update") == []


def _seed_flight(fake: FakeCalendarService) -> None:
    """Kyiv 10:00 → New York 14:00 (= 21:00 Kyiv): start and end in different zones."""
    body = {
        "summary": "Flight",
        "start": {"dateTime": "2026-11-12T10:00:00", "timeZone": "Europe/Kiev"},
        "end": {"dateTime": "2026-11-12T14:00:00", "timeZone": "America/New_York"},
    }
    fake.events().insert(calendarId="primary", body=body).execute()


async def test_moving_the_start_keeps_the_ends_own_zone(fake_factory, fake):
    _seed_flight(fake)

    await google__update_event(fake_factory, event_id="evt1", start="2026-11-13T10:00:00")

    [update] = fake.sent("events.update")
    assert update["body"]["end"] == {"dateTime": "2026-11-13T14:00:00-05:00", "timeZone": "America/New_York"}
    assert fake.event("primary", "evt1")["end"] == {
        "dateTime": "2026-11-13T21:00:00+02:00",
        "timeZone": "America/New_York",
    }


async def test_kept_duration_is_wall_clock_across_a_dst_change(fake_factory, fake):
    """Fri 09:00 → Mon 09:00 across 25 Oct (73 h elapsed) moved a week on still ends Mon 09:00."""
    fake.seed_event("primary", "Long weekend", "2026-10-23T09:00:00", "2026-10-26T09:00:00", time_zone="Europe/Kiev")

    await google__update_event(fake_factory, event_id="evt1", start="2026-10-30T09:00:00")

    event = fake.event("primary", "evt1")
    assert (event["start"]["dateTime"], event["end"]["dateTime"]) == (
        "2026-10-30T09:00:00+02:00",
        "2026-11-02T09:00:00+02:00",
    )


async def test_confirmation_marks_a_single_missing_stored_time(fake_factory, fake):
    def _response_without_end(event):
        event.pop("end", None)

    fake.on_store = _response_without_end

    result = await google__create_event(
        fake_factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    assert "Start: 2026-11-12T10:00:00+02:00 | End: (stored times unavailable) (Europe/Kiev)" in result.splitlines()


async def test_start_only_move_of_a_zero_length_event_works(fake_factory, fake):
    """Measured live: Google stores end == start (10:00→10:00) and rejects only end < start."""
    fake.seed_event("primary", "Reminder", "2026-11-12T10:00:00+02:00", "2026-11-12T10:00:00+02:00")

    await google__update_event(fake_factory, event_id="evt1", start="2026-11-12T17:00:00")

    event = fake.event("primary", "evt1")
    assert (event["start"]["dateTime"], event["end"]["dateTime"]) == (
        "2026-11-12T17:00:00+02:00",
        "2026-11-12T17:00:00+02:00",
    )


def test_fake_rejects_an_end_before_the_start(fake):
    from googleapiclient.errors import HttpError

    with pytest.raises(HttpError) as raised:
        fake.seed_event("primary", "Backwards", "2026-11-12T11:00:00+02:00", "2026-11-12T10:00:00+02:00")

    assert raised.value.status_code == 400


def test_fake_freebusy_returns_nothing_for_an_inverted_range(fake):
    # 08:00–13:00 overlaps both bounds, so only the inverted-range rule can empty the result.
    fake.seed_event("primary", "Workshop", "2026-11-12T08:00:00+02:00", "2026-11-12T13:00:00+02:00")

    result = (
        fake.freebusy()
        .query(
            body={
                "timeMin": "2026-11-12T12:00:00+02:00",
                "timeMax": "2026-11-12T09:00:00+02:00",
                "items": [{"id": "primary"}],
            }
        )
        .execute()
    )

    assert result["calendars"]["primary"]["busy"] == []


# ── Agenda (briefing) lines for events that cross the day ───────────────────


def _timed(title: str, start: str, end: str) -> dict:
    return {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}


def test_agenda_dates_an_event_that_starts_and_ends_on_other_days() -> None:
    """A Mon 09:00 → Wed 17:00 conference in Tuesday's briefing is not "09:00–17:00"."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from pincer.integrations.google.calendar_render import render_agenda

    conference = _timed("Conference", "2026-11-02T09:00:00+02:00", "2026-11-04T17:00:00+02:00")
    text = render_agenda([conference], ZoneInfo("Europe/Kyiv"), date(2026, 11, 3))
    assert text.splitlines()[1] == "  Mon 02 Nov 09:00–Wed 04 Nov 17:00  Conference"


def test_agenda_dates_only_the_end_that_falls_on_another_day() -> None:
    from datetime import date
    from zoneinfo import ZoneInfo

    from pincer.integrations.google.calendar_render import render_agenda

    zone, day = ZoneInfo("Europe/Kyiv"), date(2026, 11, 3)
    events = [
        _timed("Night shift", "2026-11-03T22:00:00+02:00", "2026-11-04T06:00:00+02:00"),
        _timed("Late call", "2026-11-03T23:00:00+02:00", "2026-11-04T00:00:00+02:00"),
        _timed("Standup", "2026-11-03T10:00:00+02:00", "2026-11-03T10:15:00+02:00"),
    ]
    assert render_agenda(events, zone, day).splitlines()[1:] == [
        "  22:00–Wed 04 Nov 06:00  Night shift",
        "  23:00–00:00  Late call",  # ending at midnight is still this day
        "  10:00–10:15  Standup",
    ]


async def test_get_shows_an_event_zone_with_the_same_offset_but_another_identity(fake_factory, fake):
    """Istanbul and Kyiv are both +03:00 in July, but Istanbul stays +03:00 in winter: a time
    rewritten in "the event's zone" would move. Only aliases of one zone (Kiev/Kyiv) are the same."""
    istanbul = fake.seed_event(
        "primary", "Ferry", "2026-07-15T10:00:00", "2026-07-15T11:00:00", time_zone="Europe/Istanbul"
    )
    alias = fake.seed_event("primary", "Lunch", "2026-07-15T12:00:00", "2026-07-15T13:00:00", time_zone="Europe/Kyiv")

    shown = (await google__get_event(fake_factory, event_id=istanbul)).splitlines()
    hidden = (await google__get_event(fake_factory, event_id=alias)).splitlines()

    assert "Start:       2026-07-15T10:00:00+03:00" in shown  # precondition: same offset as Kyiv in July
    assert "Event's own timezone: Europe/Istanbul" in shown
    assert not [line for line in hidden if line.startswith("Event's own timezone")]


async def test_list_and_search_lines_name_an_event_zone_that_is_not_the_calendars(fake_factory, fake):
    """update reads offset-less times in the event's own zone: list/search must show it when it differs."""
    fake.seed_event("primary", "Ferry", "2026-07-15T10:00:00", "2026-07-15T11:00:00", time_zone="Europe/Istanbul")
    fake.seed_event("primary", "Lunch", "2026-07-15T12:00:00", "2026-07-15T13:00:00", time_zone="Europe/Kyiv")

    listed = await google__list_events(fake_factory, time_min="2026-07-15", time_max="2026-07-15")
    found = await google__search_events(fake_factory, query="Ferry")

    for text in (listed, found):
        assert "  2026-07-15T10:00:00+03:00 → 2026-07-15T11:00:00+03:00 — Ferry | event zone Europe/Istanbul" in (
            text.splitlines()
        ), text
    lunch = [line for line in listed.splitlines() if "— Lunch" in line]
    assert lunch == ["  2026-07-15T12:00:00+03:00 → 2026-07-15T13:00:00+03:00 — Lunch"]  # an alias: no zone shown


async def test_voice_speech_still_reads_a_line_that_names_the_event_zone(fake_factory, fake):
    from pincer.voice import tool_speech

    fake.seed_event("primary", "Ferry", "2026-07-15T10:00:00", "2026-07-15T11:00:00", time_zone="Europe/Istanbul")
    listed = await google__list_events(fake_factory, time_min="2026-07-15", time_max="2026-07-15")
    assert "| event zone Europe/Istanbul" in listed  # precondition: the line carries the zone

    spoken = tool_speech.render("google__list_events", listed, "en")

    assert "Wednesday, July 15 at 10:00 AM: Ferry" in spoken, spoken
    assert "zone" not in spoken and "|" not in spoken
