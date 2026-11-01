"""
Pincer reports local, stored times (calendar reliability, slice B).

Driven through the real calendar tools against the stateful fake. The owner's
calendar is reported by Google as ``Europe/Kiev`` (legacy alias) while the
agent clock is ``Europe/Berlin`` (PINCER_TIMEZONE) or ``Europe/Kyiv`` (identity).
"""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.tools_calendar import (
    google__create_event,
    google__get_event,
    google__list_events,
    google__search_events,
    google__update_event,
    register_calendar_tools,
)
from pincer.llm.base import ToolCall
from pincer.tools.registry import ToolRegistry

KIEV_NOTE = (
    "Note: times are in this calendar's timezone (Europe/Kiev), which differs from your clock timezone (Europe/Berlin)."
)
LINK1 = "https://calendar.google.com/calendar/event?eid=evt1"


@pytest.fixture(autouse=True)
def berlin_deployment(monkeypatch):
    """PINCER_TIMEZONE=Europe/Berlin — the agent clock zone when no identity zone is set."""
    monkeypatch.setattr(
        "pincer.config.get_settings_relaxed",
        lambda: SimpleNamespace(timezone="Europe/Berlin", voice_timezone="Europe/Berlin"),
    )


@pytest.fixture
def fake():
    return FakeCalendarService({"primary": "Europe/Kiev", "berlin@group.calendar.google.com": "Europe/Berlin"})


@pytest.fixture
def factory(fake):
    return FakeCalendarFactory(fake)


@pytest.fixture
def registry(factory):
    reg = ToolRegistry()
    register_calendar_tools(reg, factory)
    return reg


def _seed_day(fake: FakeCalendarService) -> None:
    """A New-York-zoned standup (03:00 NY = 10:00 Kyiv) and an all-day holiday on 2026-11-12."""
    fake.seed_event("primary", "Standup", "2026-11-12T03:00:00", "2026-11-12T04:00:00", time_zone="America/New_York")
    fake.seed_all_day("primary", "Holiday", "2026-11-12", "2026-11-13")


# ── Reads ────────────────────────────────────────────────────────────────────


async def test_list_shows_local_times_zone_header_all_day_ids_and_note(factory, fake):
    _seed_day(fake)

    result = await google__list_events(factory, time_min="2026-11-12", time_max="2026-11-12")

    assert result == (
        "2 event(s) (2026-11-12 – 2026-11-13), times in Europe/Kiev:\n"
        f"{KIEV_NOTE}\n"
        "  2026-11-12 → 2026-11-12 — Holiday | all day\n"
        "  ID: evt2\n"
        "  2026-11-12T10:00:00+02:00 → 2026-11-12T11:00:00+02:00 — Standup\n"
        "  ID: evt1"
    )


async def test_search_shows_local_times_and_zone_header(factory, fake):
    _seed_day(fake)

    result = await google__search_events(factory, query="Standup")

    assert result.splitlines()[0] == "Found 1 event(s), times in Europe/Kiev:"
    assert "  2026-11-12T10:00:00+02:00 → 2026-11-12T11:00:00+02:00 — Standup" in result.splitlines()


async def test_get_shows_local_time_and_zone(factory, fake):
    _seed_day(fake)

    result = await google__get_event(factory, event_id="evt1")

    lines = result.splitlines()
    assert "Start:       2026-11-12T10:00:00+02:00" in lines
    assert "End:         2026-11-12T11:00:00+02:00" in lines
    assert "Time zone:   Europe/Kiev" in lines
    assert KIEV_NOTE in lines


async def test_get_all_day_event_says_all_day(factory, fake):
    _seed_day(fake)

    result = await google__get_event(factory, event_id="evt2")

    assert "Start:       2026-11-12 (all day)" in result.splitlines()
    assert "End:         2026-11-12 (all day)" in result.splitlines()


async def test_multi_day_all_day_event_shows_its_last_day_not_googles_exclusive_end(factory, fake):
    fake.seed_all_day("primary", "Trip", "2026-11-12", "2026-11-15")

    result = await google__list_events(factory, time_min="2026-11-12", time_max="2026-11-14")

    assert "  2026-11-12 → 2026-11-14 — Trip | all day" in result.splitlines()


async def test_every_listed_event_carries_its_id_line(factory, fake):
    _seed_day(fake)
    fake.seed_event("primary", "Lunch", "2026-11-12T13:00:00+02:00", "2026-11-12T14:00:00+02:00")

    result = await google__list_events(factory, time_min="2026-11-12", time_max="2026-11-12")

    ids = [line.strip() for line in result.splitlines() if line.startswith("  ID: ")]
    assert ids == ["ID: evt2", "ID: evt1", "ID: evt3"]


# ── Mismatch note ────────────────────────────────────────────────────────────


async def test_kiev_calendar_under_kyiv_identity_clock_shows_no_note(registry, fake):
    """Europe/Kiev (Google's legacy alias) and Europe/Kyiv are one zone."""
    _seed_day(fake)

    result = await registry.execute(
        "google__list_events",
        {"time_min": "2026-11-12", "time_max": "2026-11-12"},
        context={"timezone": "Europe/Kyiv"},
    )

    assert result.splitlines()[0] == "2 event(s) (2026-11-12 – 2026-11-13), times in Europe/Kiev:"
    assert "Note:" not in result


async def test_matching_zones_show_no_note(factory, fake):
    fake.seed_event("berlin@group.calendar.google.com", "Gym", "2026-11-12T18:00:00+01:00", "2026-11-12T19:00:00+01:00")

    result = await google__list_events(
        factory, calendar_id="berlin@group.calendar.google.com", time_min="2026-11-12", time_max="2026-11-12"
    )

    assert result.splitlines()[0] == "1 event(s) (2026-11-12 – 2026-11-13), times in Europe/Berlin:"
    assert "Note:" not in result


# ── Writes ───────────────────────────────────────────────────────────────────


async def test_create_confirmation_quotes_stored_times_zone_id_and_link(factory, fake):
    result = await google__create_event(
        factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    assert result == (
        "Event created: 'Dentist'\n"
        "Start: 2026-11-12T10:00:00+02:00 | End: 2026-11-12T11:00:00+02:00 (Europe/Kiev)\n"
        f"{KIEV_NOTE}\n"
        "ID: evt1\n"
        f"Link: {LINK1}"
    )


async def test_create_reports_what_google_stored_not_what_was_requested(factory, fake):
    def _google_shifts_it(event):
        for key in ("start", "end"):
            moved = (
                FakeCalendarService.instants(event)[0 if key == "start" else 1] + timedelta(minutes=30)
            ).isoformat()
            event[key]["dateTime"] = moved

    fake.on_store = _google_shifts_it

    result = await google__create_event(
        factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )

    assert "Start: 2026-11-12T10:30:00+02:00 | End: 2026-11-12T11:30:00+02:00 (Europe/Kiev)" in result.splitlines()


async def test_update_confirmation_quotes_stored_times_zone_id_and_link(factory, fake):
    await google__create_event(factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00")

    result = await google__update_event(
        factory, event_id="evt1", start="2026-11-12T17:00:00", end="2026-11-12T18:00:00"
    )

    assert result == (
        "Event updated: 'Dentist'\n"
        "Start: 2026-11-12T17:00:00+02:00 | End: 2026-11-12T18:00:00+02:00 (Europe/Kiev)\n"
        f"{KIEV_NOTE}\n"
        "ID: evt1\n"
        f"Link: {LINK1}"
    )


async def test_create_then_get_by_returned_id_returns_the_same_event(factory, fake):
    created = await google__create_event(
        factory, summary="Dentist", start="2026-11-12T10:00:00", end="2026-11-12T11:00:00"
    )
    event_id = re.search(r"^ID: (\S+)$", created, re.MULTILINE).group(1)
    created_start = re.search(r"^Start: (\S+) \|", created, re.MULTILINE).group(1)

    fetched = await google__get_event(factory, event_id=event_id)

    assert "Title:       Dentist" in fetched.splitlines()
    assert f"Start:       {created_start}" in fetched.splitlines()
    assert f"ID:          {event_id}" in fetched.splitlines()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("google__create_event", {"summary": "Dentist", "start": "2026-11-12T10:00:00", "end": "2026-11-12T11:00:00"}),
        ("google__update_event", {"event_id": "evt1", "summary": "Dentist (moved)", "start": "2026-11-12T17:00:00"}),
    ],
)
async def test_write_response_without_id_is_an_unconfirmed_error(
    registry, fake, settings, mock_llm, session_manager, cost_tracker, tool, arguments
):
    from pincer.core.agent import Agent

    fake.seed_event("primary", "Dentist", "2026-11-12T10:00:00+02:00", "2026-11-12T11:00:00+02:00")
    fake.drop_id_on_write = True
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    result = await agent._execute_tool(ToolCall(id="tc1", name=tool, arguments=dict(arguments)), "u1", "test")

    assert result.is_error is True
    assert "not confirmed" in result.content
    assert "may or may not exist" in result.content
    assert "created" not in result.content.lower()
    assert "updated" not in result.content.lower()


# ── Voice format contract on real renderer output ────────────────────────────


async def test_voice_link_extraction_reads_real_create_output(registry):
    result = await registry.execute(
        "google__create_event",
        {
            "summary": "Zahnreinigung",
            "start": "2026-08-25T14:00:00+02:00",
            "end": "2026-08-25T14:30:00+02:00",
            "timezone": "Europe/Berlin",
        },
    )

    # The exact patterns voice/scheduling.py and voice/receptionist/session.py use.
    assert re.search(r"Link: (\S+)", result).group(1) == LINK1
    assert re.search(r"Link:\s*(\S+)", result).group(1) == LINK1


async def test_voice_tool_speech_reads_real_list_output(registry, fake):
    from pincer.voice import tool_speech

    fake.seed_event("primary", "Beratung", "2026-11-12T14:00:00+02:00", "2026-11-12T15:00:00+02:00")
    fake.seed_event("primary", "Termin", "2026-11-12T16:00:00+02:00", "2026-11-12T17:00:00+02:00")
    result = await registry.execute("google__list_events", {"time_min": "2026-11-12", "time_max": "2026-11-12"})

    spoken = tool_speech.render("google__list_events", result, "en")

    assert spoken.startswith("There are 2 appointment(s): ")
    assert "Beratung" in spoken
    assert "Termin" in spoken
