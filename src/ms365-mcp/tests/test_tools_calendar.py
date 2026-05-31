"""Tests for MS365 calendar tools — one test per tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ms365.tools_calendar import (
    outlook__accept_event,
    outlook__check_availability,
    outlook__create_event,
    outlook__create_online_meeting,
    outlook__decline_event,
    outlook__delete_event,
    outlook__get_event,
    outlook__list_calendars,
    outlook__list_events,
    outlook__search_events,
    outlook__tentative_event,
    outlook__update_event,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_list_calendars(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": [{"id": "cal-1", "name": "My Calendar", "color": "auto"}]}
    result = await outlook__list_calendars(mock_client)
    assert "My Calendar" in result
    assert "1 calendar(s)" in result


@pytest.mark.asyncio
async def test_list_events(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "evt-1",
                "subject": "Team Standup",
                "start": {"dateTime": "2026-03-27T09:00:00"},
                "end": {"dateTime": "2026-03-27T09:30:00"},
                "organizer": {"emailAddress": {"name": "Alice"}},
                "location": {"displayName": "Room A"},
                "isOnlineMeeting": False,
            }
        ]
    }
    result = await outlook__list_events(mock_client)
    assert "Team Standup" in result
    assert "1 event(s)" in result


@pytest.mark.asyncio
async def test_get_event(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "evt-1",
        "subject": "Budget Review",
        "start": {"dateTime": "2026-03-27T14:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-27T15:00:00", "timeZone": "UTC"},
        "location": {"displayName": "Conference Room"},
        "body": {"content": "Review Q3 budget numbers"},
        "showAs": "busy",
        "organizer": {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
        "isOnlineMeeting": False,
        "attendees": [
            {"emailAddress": {"name": "Carol", "address": "carol@example.com"}, "status": {"response": "accepted"}}
        ],
    }
    result = await outlook__get_event(mock_client, event_id="evt-1")
    assert "Budget Review" in result
    assert "Conference Room" in result
    assert "carol@example.com" in result


@pytest.mark.asyncio
async def test_search_events(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "evt-2",
                "subject": "Sprint Planning",
                "start": {"dateTime": "2026-03-28T10:00:00"},
                "end": {"dateTime": "2026-03-28T11:00:00"},
                "organizer": {"emailAddress": {"name": "Dave"}},
                "location": {},
            }
        ]
    }
    result = await outlook__search_events(mock_client, query="Sprint")
    assert "Sprint Planning" in result


@pytest.mark.asyncio
async def test_check_availability(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {
        "value": [
            {
                "scheduleId": "alice@example.com",
                "scheduleItems": [
                    {
                        "start": {"dateTime": "2026-03-27T14:00:00"},
                        "end": {"dateTime": "2026-03-27T15:00:00"},
                        "status": "busy",
                        "subject": "Meeting",
                    }
                ],
            }
        ]
    }
    result = await outlook__check_availability(mock_client, emails="alice@example.com")
    assert "alice@example.com" in result
    assert "busy" in result


@pytest.mark.asyncio
async def test_create_event(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "new-evt", "onlineMeeting": None}
    result = await outlook__create_event(
        mock_client, subject="Review Meeting", start="2026-03-28T14:00:00", end="2026-03-28T15:00:00"
    )
    assert "Event created" in result
    assert "Review Meeting" in result
    assert "new-evt" in result


@pytest.mark.asyncio
async def test_update_event(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await outlook__update_event(mock_client, event_id="evt-1", subject="Updated Meeting")
    assert "updated" in result


@pytest.mark.asyncio
async def test_delete_event(mock_client: MagicMock) -> None:
    mock_client.delete.return_value = {}
    result = await outlook__delete_event(mock_client, event_id="evt-1")
    assert "deleted" in result


@pytest.mark.asyncio
async def test_accept_event(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__accept_event(mock_client, event_id="evt-1")
    assert "accepted" in result


@pytest.mark.asyncio
async def test_decline_event(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__decline_event(mock_client, event_id="evt-1")
    assert "declined" in result


@pytest.mark.asyncio
async def test_tentative_event(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__tentative_event(mock_client, event_id="evt-1")
    assert "tentatively accepted" in result


@pytest.mark.asyncio
async def test_create_online_meeting(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {
        "id": "meeting-1",
        "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/123",
    }
    result = await outlook__create_online_meeting(
        mock_client, subject="Team Sync", start="2026-03-28T10:00:00Z", end="2026-03-28T10:30:00Z"
    )
    assert "Team Sync" in result
    assert "teams.microsoft.com" in result
