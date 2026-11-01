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


async def test_insert_whose_connection_resets_after_commit_reports_the_event(fake_registry, fake):
    """No HTTP status, no retry: the deterministic ID is looked up before giving up (the duplicate bug:
    an error here made the model create again)."""
    fake.fail_next("events.insert", "after-commit-reset")

    result = await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))

    (event,) = fake.events_on("primary")
    assert len(fake.sent("events.insert")) == 1
    assert [g["eventId"] for g in fake.sent("events.get")] == [event["id"]]
    assert result.startswith("Event created: 'Dentist'"), result


async def test_insert_whose_connection_resets_before_commit_still_fails(fake_registry, fake):
    fake.fail_next("events.insert", "before-commit-reset")

    with pytest.raises(ConnectionResetError):
        await fake_registry.execute("google__create_event", dict(DENTIST), context=_turn("turn-a"))

    assert fake.events_on("primary") == []
    assert len(fake.sent("events.get")) == 1  # it looked, found nothing, and kept the error


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


def _delete_status(fake: FakeCalendarService, event_id: str) -> int | None:
    try:
        fake.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as exc:
        return exc.status_code
    return None


async def test_deleting_an_already_deleted_event_reports_it_gone(fake_registry, fake):
    """A first-attempt 410 is Google's "already deleted": the outcome asked for, not an error.
    (A first-attempt 404 — wrong ID or no access — stays an error: see the test below.)"""
    gone = fake.seed_event("primary", "Old", "2026-11-12T09:00:00+02:00", "2026-11-12T10:00:00+02:00")
    assert (_delete_status(fake, gone), _delete_status(fake, gone)) == (None, 410)  # precondition: the fake's 410
    assert _delete_status(fake, "nosuchevent1") == 404  # precondition: and its 404
    event_id = fake.seed_event("primary", "Dentist", "2026-11-12T15:00:00+02:00", "2026-11-12T16:00:00+02:00")
    await fake_registry.execute("google__delete_event", {"event_id": event_id}, context=_turn("turn-a"))

    again = await fake_registry.execute("google__delete_event", {"event_id": event_id}, context=_turn("turn-b"))

    assert again == f"Event {event_id} was already deleted."
    assert [d["eventId"] for d in fake.sent("events.delete")].count(event_id) == 2


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


# ── Review fixes CR1, CR2, minors 4 and 8 ────────────────────────────────────


async def _create(fake_registry, turn: str, call: str, **overrides) -> str:
    return await fake_registry.execute("google__create_event", {**DENTIST, **overrides}, context=_turn(turn, call))


async def test_recreate_after_delete_survives_a_lost_insert_response(fake_registry, fake):
    """create X → delete X → create again (→ Y) whose response is lost after commit."""
    await _create(fake_registry, "turn-a", "c1")
    [first] = fake.events_on("primary")
    await fake_registry.execute("google__delete_event", {"event_id": first["id"]}, context=_turn("turn-a", "c2"))
    # X's insert is a 409 (raised before any commit), so this fault lands on Y's insert.
    fake.fail_next("events.insert", "after-commit")

    result = await _create(fake_registry, "turn-a", "c3")

    [live] = fake.events_on("primary")
    assert live["id"] != first["id"]
    assert fake.faults["events.insert"] == []  # the fault was consumed by Y's insert
    assert result.startswith("Event created: 'Dentist'")
    assert f"ID: {live['id']}" in result.splitlines()


async def test_identical_call_after_a_recreate_finds_the_live_event(fake_registry, fake):
    await _create(fake_registry, "turn-a", "c1")
    [first] = fake.events_on("primary")
    await fake_registry.execute("google__delete_event", {"event_id": first["id"]}, context=_turn("turn-a", "c2"))
    await _create(fake_registry, "turn-a", "c3")
    [live] = fake.events_on("primary")

    again = await _create(fake_registry, "turn-a", "c4")

    assert len(fake.events_on("primary")) == 1
    assert again.startswith("Event already created: 'Dentist'")
    assert f"ID: {live['id']}" in again.splitlines()


async def test_a_reserved_but_purged_id_does_not_block_a_keyed_booking(fake_registry, fake):
    """Google keeps a purged event's ID reserved (409) but no longer serves it (GET 404)."""
    keyed = {"idempotency_key": "pincer-appointment-task-9"}
    await _create(fake_registry, "turn-a", "c1", **keyed)
    [first] = fake.events_on("primary")
    fake.purge("primary", first["id"])

    result = await _create(fake_registry, "turn-b", "c2", **keyed)

    [live] = fake.events_on("primary")
    assert live["id"] != first["id"]
    assert result.startswith("Event created: 'Dentist'")


async def test_ids_exhausted_by_deleted_events_give_a_clear_error(fake_registry, fake):
    for call in ("c1", "c3", "c5"):
        await _create(fake_registry, "turn-a", call)
        [event] = fake.events_on("primary")
        await fake_registry.execute(
            "google__delete_event", {"event_id": event["id"]}, context=_turn("turn-a", call + "d")
        )

    with pytest.raises(RuntimeError, match="Google keeps their IDs reserved"):
        await _create(fake_registry, "turn-a", "c7")

    assert fake.events_on("primary") == []


async def test_title_whitespace_does_not_defeat_dedup(fake_registry, fake):
    await _create(fake_registry, "turn-a", "c1", summary="Dentist ")
    again = await _create(fake_registry, "turn-a", "c2", summary="  Dentist")

    assert len(fake.events_on("primary")) == 1
    assert again.startswith("Event already created:")


async def test_retried_delete_verification_survives_a_transient_get_failure(fake_registry, fake):
    event_id = fake.seed_event("primary", "Dentist", "2026-11-12T15:00:00+02:00", "2026-11-12T16:00:00+02:00")
    fake.fail_next("events.delete", "after-commit")
    fake.fail_next("events.get", "before-commit")

    result = await fake_registry.execute("google__delete_event", {"event_id": event_id}, context=_turn("turn-a"))

    assert result == f"Event {event_id} deleted."


# ── Full-branch review N5: a same-turn re-call with other details ────────────


async def test_a_same_turn_re_call_with_other_details_says_they_were_not_applied(fake_registry, fake):
    """The dedup ID covers title and times only: new attendees/description on the re-call are dropped,
    and the model must hear that rather than a plain "already created"."""
    await _create(fake_registry, "turn-a", "c1")

    again = await _create(
        fake_registry, "turn-a", "c2", description="Bring the X-ray", attendees="ana@example.com", add_meet_link=True
    )

    (event,) = fake.events_on("primary")
    assert "description" not in event and "attendees" not in event  # precondition: nothing was applied
    assert again.startswith("Event already created: 'Dentist'")
    assert (
        "Not applied: description, attendees, Google Meet link differ from the existing event; "
        "call google__update_event with its ID to change them."
    ) in again


async def test_an_identical_same_turn_re_call_says_nothing_about_unapplied_fields(fake_registry, fake):
    await _create(fake_registry, "turn-a", "c1", description="Bring the X-ray")

    again = await _create(fake_registry, "turn-a", "c2", description="Bring the X-ray")

    assert again.startswith("Event already created: 'Dentist'")
    assert "Not applied" not in again
