"""
Edits steered to the update tool (calendar reliability, slice D, Q10).

The owner's screenshot ("Meeting" + a fresh "Meeting (updated)" in the same
slot) was the model creating because it had no event ID in hand. The create
tool must say it is only for new events, the update tool must name the edits
it is for, and every read result that shows events must end with a one-line
hint pointing changes at the update tool with the event's ID.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.tools_calendar import register_calendar_tools
from pincer.tools.registry import ToolRegistry

DAY = "2026-11-12"


@pytest.fixture(autouse=True)
def berlin_deployment(monkeypatch):
    monkeypatch.setattr(
        "pincer.config.get_settings_relaxed",
        lambda: SimpleNamespace(timezone="Europe/Berlin", voice_timezone="Europe/Berlin"),
    )


@pytest.fixture
def fake() -> FakeCalendarService:
    service = FakeCalendarService({"primary": "Europe/Kyiv"})
    service.seed_event("primary", "Meeting", f"{DAY}T10:00:00+02:00", f"{DAY}T11:00:00+02:00")
    return service


@pytest.fixture
def registry(fake) -> ToolRegistry:
    reg = ToolRegistry()
    register_calendar_tools(reg, FakeCalendarFactory(fake))
    return reg


def _description(registry: ToolRegistry, name: str) -> str:
    (schema,) = [s for s in registry.get_schemas() if s["name"] == name]
    return str(schema["description"]).lower()


def _is_edit_hint(line: str) -> bool:
    lowered = line.lower()
    return "google__update_event" in line and " id" in lowered and "never create" in lowered


def test_create_description_is_only_for_new_events(registry) -> None:
    text = _description(registry, "google__create_event")
    assert "new event" in text
    assert "never" in text
    for edit in ("change", "rename", "reschedule"):
        assert edit in text, edit
    assert "google__update_event" in text


def test_update_description_names_the_edits(registry) -> None:
    text = _description(registry, "google__update_event")
    for edit in ("rename", "reschedule", "change the time", "change details"):
        assert edit in text, edit


def test_move_description_is_only_for_changing_calendars(registry) -> None:
    """ "Move my 10am" ranked this tool first: it must say it does not change the time."""
    text = _description(registry, "google__move_event")
    assert "different calendar" in text
    assert "not for changing the time" in text
    assert "reschedule" in text
    assert "google__update_event" in text


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("google__list_events", {"time_min": DAY, "time_max": DAY}),
        ("google__search_events", {"query": "Meeting"}),
        ("google__get_event", {"event_id": "evt1"}),
    ],
)
async def test_read_results_with_events_end_with_the_edit_hint(registry, tool, args) -> None:
    text = await registry.execute(tool, args)
    assert "Meeting" in text  # the read really returned the event
    assert _is_edit_hint(text.splitlines()[-1]), text


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("google__list_events", {"time_min": "2026-11-20", "time_max": "2026-11-20"}),
        ("google__search_events", {"query": "Nothing like this"}),
        ("google__check_freebusy", {"emails": "primary", "time_min": DAY, "time_max": DAY}),
    ],
)
async def test_no_hint_without_events_or_on_freebusy(registry, tool, args) -> None:
    text = await registry.execute(tool, args)
    assert text.strip()
    assert not any(_is_edit_hint(line) for line in text.splitlines()), text


async def test_hint_is_not_parsed_as_an_event_by_voice(registry) -> None:
    """The voice list renderer must still count exactly one event."""
    from pincer.voice.tool_speech import _render_list_events

    text = await registry.execute("google__list_events", {"time_min": DAY, "time_max": DAY})
    spoken = _render_list_events(text, "en")
    assert "Meeting" in spoken
    assert spoken.count("Meeting") == 1
