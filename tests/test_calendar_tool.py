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

    async def test_day_heading_agrees_with_converted_times(self):
        """A 00:30 Europe/Berlin event returned as 23:30Z the previous UTC day
        must be filed under Tuesday, matching the 00:30 it displays."""
        events = [
            {
                "summary": "Night Shift",
                "start": {"dateTime": "2026-08-17T22:30:00Z"},
                "end": {"dateTime": "2026-08-17T23:30:00Z"},
            },
        ]
        mock_service = _mock_events_list(events)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_week

            result = await calendar_week()
            assert "00:30 - 01:30" in result  # CEST is UTC+2
            assert "Tuesday, August 18" in result
            assert "Monday, August 17" not in result


@pytest.mark.asyncio
class TestCalendarCreate:
    async def test_create_event(self):
        created = {
            "id": "evt_123",
            "summary": "New Meeting",
            "start": {"dateTime": "2026-02-22T14:00:00+01:00"},
            "end": {"dateTime": "2026-02-22T15:00:00+01:00"},
            "htmlLink": "https://calendar.google.com/event/123",
        }
        mock_service = _mock_events_insert(created)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_create

            result = await calendar_create("New Meeting", "2026-02-22T14:00:00+01:00")
            assert "created" in result.lower()
            assert "New Meeting" in result
            assert "https://calendar.google.com/event/123" in result

    async def test_create_event_api_missing_id_and_link(self):
        """API returns empty or partial response -> tool returns error, not success."""
        for created in ({}, {"summary": "x"}):
            mock_service = _mock_events_insert(created)
            with (
                patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
                patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
            ):
                mock_s.return_value.timezone = "Europe/Berlin"
                from pincer.tools.builtin.calendar_tool import calendar_create

                result = await calendar_create("Test", "2026-02-22T14:00:00+01:00")
                assert "Event created" not in result
                assert "Error" in result or "did not return" in result

    async def test_create_event_success_includes_link(self):
        """Successful creation returns htmlLink in result."""
        created = {
            "id": "abc",
            "htmlLink": "https://calendar.google.com/event/xyz",
        }
        mock_service = _mock_events_insert(created)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_create

            result = await calendar_create("Meeting", "2026-02-22T14:00:00+01:00")
            assert "https://calendar.google.com/event/xyz" in result
            assert "created" in result.lower()

    async def test_create_event_naive_datetime_uses_settings_timezone(self):
        """Naive datetime -> event body uses settings.timezone (IANA)."""
        created = {"id": "x", "htmlLink": "https://example.com/event"}
        mock_service = _mock_events_insert(created)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_create

            await calendar_create("Test", "2026-02-22T14:00:00")
            call_kwargs = mock_service.events.return_value.insert.call_args[1]
            body = call_kwargs["body"]
            assert body["start"]["timeZone"] == "Europe/Berlin"
            assert body["end"]["timeZone"] == "Europe/Berlin"

    async def test_create_event_offset_aware_uses_settings_timezone(self):
        """Fixed-offset datetime (e.g. +01:00) -> use settings.timezone for IANA."""
        created = {"id": "x", "htmlLink": "https://example.com/event"}
        mock_service = _mock_events_insert(created)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "America/New_York"
            from pincer.tools.builtin.calendar_tool import calendar_create

            await calendar_create("Test", "2026-02-22T14:00:00+01:00")
            call_kwargs = mock_service.events.return_value.insert.call_args[1]
            body = call_kwargs["body"]
            assert body["start"]["timeZone"] == "America/New_York"

    async def test_create_event_non_primary_calendar_includes_calendar_id(self):
        """When calendar_id != primary, success message includes calendar_id."""
        created = {"id": "x", "htmlLink": "https://example.com/event"}
        mock_service = _mock_events_insert(created)
        with (
            patch("pincer.tools.builtin.calendar_tool._get_service", return_value=mock_service),
            patch("pincer.tools.builtin.calendar_tool.get_settings") as mock_s,
        ):
            mock_s.return_value.timezone = "Europe/Berlin"
            from pincer.tools.builtin.calendar_tool import calendar_create

            result = await calendar_create("Test", "2026-02-22T14:00:00+01:00", calendar_id="work@example.com")
            assert "work@example.com" in result
            assert "Calendar:" in result

    async def test_invalid_date(self):
        with patch("pincer.tools.builtin.calendar_tool._get_service", return_value=MagicMock()):
            from pincer.tools.builtin.calendar_tool import calendar_create

            result = await calendar_create("Test", "not-a-date")
            assert "Invalid" in result or "Error" in result


class TestEnvTokenCredentials:
    """PINCER_GOOGLE_TOKEN_JSON — credentials come from memory, nothing is written."""

    def _settings(self, tmp_path, token):
        from pydantic import SecretStr

        from pincer.integrations.google.token_source import encode_json_blob

        settings = MagicMock()
        settings.google_oauth_dir.return_value = tmp_path
        settings.google_token_json = SecretStr(encode_json_blob(token))
        return settings

    def test_env_token_used_without_files(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        token = {
            "token": "ya29.env",
            "refresh_token": "1//rt",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "cs",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "expiry": "2000-01-01T00:00:00Z",
        }

        def _refresh(self, request):
            self.token = "ya29.refreshed"
            self.expiry = None

        cal._auth_cache = None
        with (
            patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path, token)),
            patch("google.oauth2.credentials.Credentials.refresh", _refresh),
            patch("google.auth.transport.requests.Request"),
        ):
            creds = cal._get_credentials()
            assert cal._get_credentials() is creds  # refreshed token cached in memory
        cal._auth_cache = None

        assert creds.token == "ya29.refreshed"
        assert list(tmp_path.iterdir()) == []

    def test_env_token_refresh_failure_raises_file_not_found(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        token = {"refresh_token": "1//rt", "client_id": "cid", "client_secret": "cs", "expiry": "2000-01-01T00:00:00Z"}
        cal._auth_cache = None
        with (
            patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path, token)),
            patch("google.oauth2.credentials.Credentials.refresh", side_effect=Exception("invalid_grant")),
            patch("google.auth.transport.requests.Request"),
            pytest.raises(FileNotFoundError, match="PINCER_GOOGLE_TOKEN_JSON"),
        ):
            cal._get_credentials()
        cal._auth_cache = None

    def test_cache_key_does_not_hold_plaintext_token(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        token = {"token": "ya29.env", "refresh_token": "1//plaintext-rt", "client_id": "c", "client_secret": "s"}
        cal._auth_cache = None
        auth = cal._auth_for({"token_info": token})
        assert cal._auth_for({"token_info": dict(token)}) is auth  # same token → cached instance
        assert "plaintext-rt" not in cal._auth_cache[0]
        assert cal._auth_for({"token_info": {**token, "refresh_token": "other"}}) is not auth
        cal._auth_cache = None


class TestFileTokenCredentials:
    """File mode goes through locate_token() too: workspace token first, then the legacy one."""

    def _write(self, path, scopes, token="ya29.file"):
        import json

        path.write_text(
            json.dumps(
                {
                    "token": token,
                    "refresh_token": "1//rt",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "cid",
                    "client_secret": "cs",
                    "scopes": scopes,
                    "expiry": "2099-01-01T00:00:00Z",
                }
            )
        )

    def _settings(self, tmp_path):
        settings = MagicMock()
        settings.google_oauth_dir.return_value = tmp_path
        return settings

    def test_prefers_workspace_token_over_legacy(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal
        from pincer.integrations.google.auth import ALL_SCOPES

        self._write(tmp_path / "google_token.json", cal.SCOPES, token="ya29.legacy")
        self._write(tmp_path / "google_workspace_token.json", ALL_SCOPES, token="ya29.workspace")
        cal._auth_cache = None
        with patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path)):
            assert cal._get_credentials().token == "ya29.workspace"
        cal._auth_cache = None

    def test_legacy_token_without_credentials_file(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        self._write(tmp_path / "google_token.json", cal.SCOPES, token="ya29.legacy")
        cal._auth_cache = None
        with patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path)):
            assert cal._get_credentials().token == "ya29.legacy"
        cal._auth_cache = None

    def test_no_token_raises_setup_required(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        cal._auth_cache = None
        with (
            patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path)),
            pytest.raises(FileNotFoundError, match="pincer google setup"),
        ):
            cal._get_credentials()

    def test_token_without_calendar_scope_raises_file_not_found(self, tmp_path):
        import pincer.tools.builtin.calendar_tool as cal

        self._write(tmp_path / "google_workspace_token.json", ["https://www.googleapis.com/auth/gmail.send"])
        cal._auth_cache = None
        with (
            patch("pincer.tools.builtin.calendar_tool.get_settings", return_value=self._settings(tmp_path)),
            pytest.raises(FileNotFoundError, match="missing required scopes"),
        ):
            cal._get_credentials()
        cal._auth_cache = None
