"""
Traceable, deduplicated creates (calendar reliability, slice C).

Driven through the real calendar tools (via the registry, with the tool context
the agent passes) against the stateful fake, which refuses a reused event ID
with 409 and can lose a response after committing (503-after-commit).
"""

from __future__ import annotations

import re

import pytest
from googleapiclient.errors import HttpError

from integrations.google.fake_calendar import FakeCalendarService

WORK_CAL = "work@group.calendar.google.com"
DENTIST = {"summary": "Dentist", "start": "2026-11-12T15:00:00", "end": "2026-11-12T16:00:00"}

pytestmark = pytest.mark.usefixtures("berlin_deployment", "instant_backoff")


@pytest.fixture
def fake():
    return FakeCalendarService({"primary": "Europe/Kiev", WORK_CAL: "Europe/Kiev"})


def _turn(turn_id: str, call_id: str = "toolu_1") -> dict[str, str]:
    return {
        "user_id": "u1",
        "pincer_user_id": "u1",
        "timezone": "Europe/Kyiv",
        "turn_id": turn_id,
        "tool_call_id": call_id,
    }


async def test_insert_whose_response_is_lost_after_commit_leaves_one_event(fake_registry, fake):
    fake.fail_next("events.insert", "after-commit")

    result = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))

    assert len(fake.events_on("primary")) == 1
    assert len(fake.sent("events.insert")) == 2
    assert result.startswith(
        "Event created: 'Dentist'\nStart: 2026-11-12T15:00:00+02:00 | End: 2026-11-12T16:00:00+02:00"
    )


async def test_insert_that_fails_before_commit_is_retried_into_one_event(fake_registry, fake):
    fake.fail_next("events.insert", "before-commit")

    result = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))

    assert len(fake.events_on("primary")) == 1
    assert result.startswith("Event created: 'Dentist'")


async def test_same_details_twice_in_one_turn_make_one_event(fake_registry, fake):
    first = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a", "toolu_1"))
    second = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a", "toolu_2"))

    [event] = fake.events_on("primary")
    assert first.startswith("Event created: 'Dentist'")
    assert second.splitlines()[:2] == [
        "Event already created: 'Dentist' (it already exists — not duplicated)",
        "Start: 2026-11-12T15:00:00+02:00 | End: 2026-11-12T16:00:00+02:00 (Europe/Kiev)",
    ]
    assert f"ID: {event['id']}" in second.splitlines()


async def test_same_details_in_a_later_turn_make_a_second_event(fake_registry, fake):
    await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))
    later = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-b"))

    assert len(fake.events_on("primary")) == 2
    assert later.startswith("Event created: 'Dentist'")


async def test_explicit_idempotency_key_dedups_across_turns(fake_registry, fake):
    args = {**DENTIST, "idempotency_key": "pincer-appointment-task-1"}

    await fake_registry.execute("google__create_event", dict(args), context=_turn("turn-a"))
    again = await fake_registry.execute("google__create_event", dict(args), context=_turn("turn-b"))

    assert len(fake.events_on("primary")) == 1
    assert again.startswith("Event already created: 'Dentist'")


async def test_same_instant_written_differently_is_the_same_event(fake_registry, fake):
    """The dedup ID hashes the instant, not the spelling: 15:00 Kyiv == 13:00Z."""
    await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))
    again = await fake_registry.execute(
        "google__create_event",
        {"summary": "Dentist", "start": "2026-11-12T13:00:00Z", "end": "2026-11-12T14:00:00Z"},
        context=_turn("turn-a", "toolu_2"),
    )

    assert len(fake.events_on("primary")) == 1
    assert again.startswith("Event already created: 'Dentist'")


async def test_generated_event_ids_are_base32hex_within_googles_length_limits(fake_registry, fake):
    from pincer.integrations.google.calendar_writer import client_event_id

    await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))
    [event] = fake.events_on("primary")
    direct = client_event_id("seed", "primary", {"summary": "x", "start": {"date": "2026-11-12"}})

    for generated in (event["id"], direct):
        assert re.fullmatch(r"[0-9a-v]{5,1024}", generated), generated
    assert [s["body"]["id"] for s in fake.sent("events.insert")] == [event["id"]]


# ── Retried delete / move ────────────────────────────────────────────────────


async def test_delete_whose_first_response_is_lost_reports_success(fake_registry, fake):
    event_id = fake.seed_event("primary", "Dentist", "2026-11-12T15:00:00+02:00", "2026-11-12T16:00:00+02:00")
    fake.fail_next("events.delete", "after-commit")

    result = await fake_registry.execute("google__delete_event", {"event_id": event_id}, context=_turn("turn-a"))

    assert result == f"Event {event_id} deleted."
    assert len(fake.sent("events.delete")) == 2
    assert fake.events_on("primary") == []


async def test_move_whose_first_response_is_lost_reports_success(fake_registry, fake):
    event_id = fake.seed_event("primary", "Dentist", "2026-11-12T15:00:00+02:00", "2026-11-12T16:00:00+02:00")
    fake.fail_next("events.move", "after-commit")

    result = await fake_registry.execute(
        "google__move_event", {"event_id": event_id, "destination_calendar_id": WORK_CAL}, context=_turn("turn-a")
    )

    assert result == f"Event {event_id} moved to {WORK_CAL} (new id={event_id})"
    assert len(fake.sent("events.move")) == 2
    assert [e["id"] for e in fake.events_on(WORK_CAL)] == [event_id]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("google__delete_event", {"event_id": "nosuchevent1"}),
        ("google__move_event", {"event_id": "nosuchevent1", "destination_calendar_id": WORK_CAL}),
    ],
)
async def test_not_found_on_the_first_attempt_is_still_an_error(fake_registry, fake, tool, arguments):
    with pytest.raises(HttpError) as raised:
        await fake_registry.execute(tool, dict(arguments), context=_turn("turn-a"))

    assert raised.value.status_code == 404
