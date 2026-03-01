"""Tests for Google Calendar tool."""

from unittest.mock import MagicMock, patch

import pytest


def _mock_events_list(events):
    """Create a mock Google Calendar events().list().execute() chain."""
    mock_service = MagicMock()
    mock_list = MagicMock()
    mock_list.execute.return_value = {"items": events}
    mock_service.events.return_value.list.return_value = mock_list
    return mock_service


def _mock_events_insert(created_event):
    mock_service = MagicMock()
    mock_insert = MagicMock()
    mock_insert.execute.return_value = created_event
    mock_service.events.return_value.insert.return_value = mock_insert
    return mock_service


@pytest.mark.asyncio
class TestCalendarToday:
    async def test_no_events(self):
        mock_service = _mock_events_list([])
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service):
            from pincer.tools.builtin.calendar_tool import calendar_today
            result = await calendar_today()
            assert "clear" in result.lower()

    async def test_with_events(self):
        events = [
            {
                "summary": "Team Standup",
                "start": {"dateTime": "2026-02-21T09:00:00+01:00"},
                "end": {"dateTime": "2026-02-21T09:30:00+01:00"},
            },
            {
                "summary": "Lunch",
                "start": {"dateTime": "2026-02-21T12:00:00+01:00"},
                "end": {"dateTime": "2026-02-21T13:00:00+01:00"},
            },
        ]
        mock_service = _mock_events_list(events)
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service):
            from pincer.tools.builtin.calendar_tool import calendar_today
            result = await calendar_today()
            assert "Team Standup" in result
            assert "Lunch" in result
            assert "2 event(s)" in result

    async def test_all_day_event(self):
        events = [{"summary": "Holiday", "start": {"date": "2026-02-21"}, "end": {"date": "2026-02-22"}}]
        mock_service = _mock_events_list(events)
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service):
            from pincer.tools.builtin.calendar_tool import calendar_today
            result = await calendar_today()
            assert "Holiday" in result
            assert "All day" in result

    async def test_credentials_missing(self):
        with patch(
            "pincer.tools.builtin.calendar_tool._get_service",
            side_effect=FileNotFoundError("Credentials not found"),
        ):
            from pincer.tools.builtin.calendar_tool import calendar_today
            result = await calendar_today()
            assert "not found" in result.lower()


@pytest.mark.asyncio
class TestCalendarWeek:
    async def test_no_events(self):
        mock_service = _mock_events_list([])
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service):
            from pincer.tools.builtin.calendar_tool import calendar_week
            result = await calendar_week()
            assert "No events" in result

    async def test_with_events(self):
        events = [
            {
                "summary": "Monday Meeting",
                "start": {"dateTime": "2026-02-23T10:00:00+01:00"},
                "end": {"dateTime": "2026-02-23T11:00:00+01:00"},
            },
        ]
        mock_service = _mock_events_list(events)
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service):
            from pincer.tools.builtin.calendar_tool import calendar_week
            result = await calendar_week()
            assert "Monday Meeting" in result


@pytest.mark.asyncio
class TestCalendarCreate:
    async def test_create_event(self):
        created = {
            "summary": "New Meeting",
            "start": {"dateTime": "2026-02-22T14:00:00+01:00"},
            "end": {"dateTime": "2026-02-22T15:00:00+01:00"},
            "htmlLink": "https://calendar.google.com/event/123",
        }
        mock_service = _mock_events_insert(created)
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service), \
             patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s:
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_create
            result = await calendar_create("New Meeting", "2026-02-22T14:00:00+01:00")
            assert "created" in result.lower()
            assert "New Meeting" in result

    async def test_invalid_date(self):
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=MagicMock()):
            from pincer.tools.builtin.calendar_tool import calendar_create
            result = await calendar_create("Test", "not-a-date")
            assert "Invalid" in result or "Error" in result
