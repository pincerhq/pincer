"""
Tests for Calendar tools — one test per tool (12 tools).
"""

from __future__ import annotations

import pytest

from pincer.integrations.google.tools_calendar import (
    google__accept_event,
    google__add_google_meet,
    google__check_freebusy,
    google__create_event,
    google__decline_event,
    google__delete_event,
    google__get_event,
    google__list_calendars,
    google__list_events,
    google__move_event,
    google__search_events,
    google__update_event,
)


def _event(event_id="evt1", summary="Team Sync"):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": "2026-03-28T14:00:00Z"},
        "end": {"dateTime": "2026-03-28T15:00:00Z"},
        "status": "confirmed",
        "organizer": {"email": "alice@example.com"},
        "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
        "attendees": [{"email": "bob@example.com", "responseStatus": "accepted", "self": True}],
    }


async def test_list_calendars(mock_factory, mock_calendar_service):
    mock_calendar_service.calendarList().list().execute.return_value = {
        "items": [{"id": "primary", "summary": "My Calendar", "timeZone": "UTC"}]
    }
    result = await google__list_calendars(mock_factory)
    assert "My Calendar" in result


async def test_list_calendars_empty(mock_factory, mock_calendar_service):
    mock_calendar_service.calendarList().list().execute.return_value = {"items": []}
    result = await google__list_calendars(mock_factory)
    assert "No calendars" in result


async def test_list_events_with_results(mock_factory, mock_calendar_service):
    mock_calendar_service.events().list().execute.return_value = {
        "items": [_event("e1", "Meeting"), _event("e2", "Standup")]
    }
    result = await google__list_events(mock_factory)
    assert "Meeting" in result
    assert "Standup" in result


async def test_list_events_empty(mock_factory, mock_calendar_service):
    mock_calendar_service.events().list().execute.return_value = {"items": []}
    result = await google__list_events(mock_factory)
    assert "No events" in result


async def test_get_event_returns_details(mock_factory, mock_calendar_service):
    mock_calendar_service.events().get().execute.return_value = _event()
    result = await google__get_event(mock_factory, event_id="evt1")
    assert "Team Sync" in result
    assert "alice@example.com" in result
    assert "bob@example.com" in result


async def test_search_events_found(mock_factory, mock_calendar_service):
    mock_calendar_service.events().list().execute.return_value = {
        "items": [_event("e1", "Budget Review")]
    }
    result = await google__search_events(mock_factory, query="budget")
    assert "Budget Review" in result


async def test_check_freebusy(mock_factory, mock_calendar_service):
    mock_calendar_service.freebusy().query().execute.return_value = {
        "calendars": {
            "alice@example.com": {
                "busy": [{"start": "2026-03-28T14:00:00Z", "end": "2026-03-28T15:00:00Z"}]
            }
        }
    }
    result = await google__check_freebusy(mock_factory, emails="alice@example.com")
    assert "BUSY" in result
    assert "alice@example.com" in result


async def test_create_event(mock_factory, mock_calendar_service):
    mock_calendar_service.events().insert().execute.return_value = {
        "id": "new_evt",
        "htmlLink": "https://cal.google.com/event?eid=new_evt",
    }
    result = await google__create_event(
        mock_factory,
        summary="Budget Review",
        start="2026-03-28T14:00:00Z",
        end="2026-03-28T15:00:00Z",
    )
    assert "Budget Review" in result
    assert "new_evt" in result


async def test_update_event(mock_factory, mock_calendar_service):
    mock_calendar_service.events().get().execute.return_value = _event()
    mock_calendar_service.events().update().execute.return_value = _event(summary="Updated Meeting")
    result = await google__update_event(mock_factory, event_id="evt1", summary="Updated Meeting")
    assert "Updated Meeting" in result or "updated" in result.lower()


async def test_delete_event(mock_factory, mock_calendar_service):
    mock_calendar_service.events().delete().execute.return_value = None
    result = await google__delete_event(mock_factory, event_id="evt1")
    assert "evt1" in result
    assert "deleted" in result.lower()


async def test_move_event(mock_factory, mock_calendar_service):
    mock_calendar_service.events().move().execute.return_value = {"id": "evt1"}
    result = await google__move_event(
        mock_factory, event_id="evt1", destination_calendar_id="work@group.calendar.google.com"
    )
    assert "moved" in result.lower()


async def test_accept_event(mock_factory, mock_calendar_service):
    evt = _event()
    mock_calendar_service.events().get().execute.return_value = evt
    mock_calendar_service.calendarList().get().execute.return_value = {"id": "primary"}
    mock_calendar_service.events().patch().execute.return_value = evt
    result = await google__accept_event(mock_factory, event_id="evt1")
    assert "Accepted" in result or "accepted" in result.lower()


async def test_decline_event(mock_factory, mock_calendar_service):
    evt = _event()
    mock_calendar_service.events().get().execute.return_value = evt
    mock_calendar_service.events().patch().execute.return_value = evt
    result = await google__decline_event(mock_factory, event_id="evt1")
    assert "Declined" in result or "declined" in result.lower()


async def test_add_google_meet(mock_factory, mock_calendar_service):
    mock_calendar_service.events().patch().execute.return_value = {
        "conferenceData": {
            "entryPoints": [{"uri": "https://meet.google.com/abc-defg-hij"}]
        }
    }
    result = await google__add_google_meet(mock_factory, event_id="evt1")
    assert "meet.google.com" in result
