"""Tests for MS365 authentication."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ms365.auth import (
    SERVICE_SCOPES,
    MS365Auth,
    MS365AuthError,
    scopes_for_services,
)


def test_scopes_for_all_services() -> None:
    result = scopes_for_services(None)
    assert "User.Read" in result
    for scopes in SERVICE_SCOPES.values():
        for scope in scopes:
            assert scope in result


def test_scopes_for_email_service() -> None:
    result = scopes_for_services(["email"])
    assert "Mail.ReadWrite" in result
    assert "Mail.Send" in result
    assert "User.Read" in result
    assert "Calendars.ReadWrite" not in result


def test_scopes_deduplicated() -> None:
    result = scopes_for_services(["email", "email"])
    assert sum(1 for s in result if s == "Mail.ReadWrite") == 1


def test_scopes_complete() -> None:
    result = scopes_for_services(None)
    assert "User.Read" in result
    assert "Mail.ReadWrite" in result
    assert "Mail.Send" in result
    assert "Calendars.ReadWrite" in result
    assert "Files.ReadWrite.All" in result


def test_service_scopes_cover_all_services() -> None:
    expected = {"email", "calendar", "onedrive", "todo", "contacts", "onenote", "directory"}
    assert set(SERVICE_SCOPES.keys()) == expected


def test_tenant_default() -> None:
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id")
    assert auth.tenant_id == "common"


def test_tenant_custom() -> None:
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id", tenant_id="my-tenant-id")
    assert auth.tenant_id == "my-tenant-id"


def test_cache_path_default() -> None:
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id")
    assert auth.cache_path == Path.home() / ".pincer" / "ms365_token_cache.json"


def test_cache_path_custom(tmp_path: Path) -> None:
    custom = str(tmp_path / "tokens.json")
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id", cache_path=custom)
    assert auth.cache_path == Path(custom)


@pytest.mark.asyncio
async def test_get_token_no_cache_raises() -> None:
    mock_msal = MagicMock()
    mock_cache = MagicMock()
    mock_cache.has_state_changed = False
    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_msal.SerializableTokenCache.return_value = mock_cache
    mock_msal.PublicClientApplication.return_value = mock_app

    with patch.dict("sys.modules", {"msal": mock_msal}):
        auth = MS365Auth.__new__(MS365Auth)
        auth._client_id = "test-id"
        auth._tenant_id = "common"
        auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
        auth._scopes = ["User.Read"]
        auth._app = None
        auth._cache = None
        auth._pending_flow_message = ""

        with pytest.raises(MS365AuthError, match="No valid Microsoft 365 token"):
            await auth.get_token()


def _bare_auth() -> MS365Auth:
    """Build an MS365Auth with private attrs set directly (no msal import needed)."""
    auth = MS365Auth.__new__(MS365Auth)
    auth._client_id = "test-id"
    auth._tenant_id = "common"
    auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
    auth._scopes = ["User.Read"]
    auth._app = MagicMock()
    auth._cache = MagicMock()
    auth._cache.has_state_changed = False
    auth._pending_flow_message = ""
    return auth


def test_initiate_device_flow_sync_returns_flow_without_blocking() -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {
        "user_code": "ABC123",
        "message": "To sign in, use code ABC123",
        "expires_in": 900,
    }

    flow = auth.initiate_device_flow_sync()

    assert flow["user_code"] == "ABC123"
    assert "expires_at" in flow
    assert auth._pending_flow_message == "To sign in, use code ABC123"
    auth._app.acquire_token_by_device_flow.assert_not_called()


def test_initiate_device_flow_sync_raises_without_user_code() -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {"error_description": "bad client"}

    with pytest.raises(MS365AuthError, match="Device flow failed"):
        auth.initiate_device_flow_sync()


def test_complete_device_flow_sync_success_saves_cache() -> None:
    auth = _bare_auth()
    auth._cache.has_state_changed = True
    auth._cache.serialize.return_value = '{"fake": "cache"}'
    auth._app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}

    result = auth.complete_device_flow_sync({"device_code": "xyz"})

    assert result["access_token"] == "new-token"
    auth._cache.serialize.assert_called_once()
    assert auth._cache_path.read_text() == '{"fake": "cache"}'
    auth._cache_path.unlink()


def test_complete_device_flow_sync_failure_raises() -> None:
    auth = _bare_auth()
    auth._app.acquire_token_by_device_flow.return_value = {
        "error": "authorization_declined",
        "error_description": "user declined",
    }

    with pytest.raises(MS365AuthError, match="authorization_declined"):
        auth.complete_device_flow_sync({"device_code": "xyz"})


def test_device_code_flow_sync_runs_initiate_then_complete(capsys: pytest.CaptureFixture[str]) -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {
        "user_code": "ABC123",
        "message": "To sign in, use code ABC123",
        "expires_in": 900,
    }
    auth._app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}

    result = auth.device_code_flow_sync()

    assert result["access_token"] == "new-token"
    captured = capsys.readouterr()
    assert "ABC123" in captured.err


@pytest.mark.asyncio
async def test_get_token_from_cache() -> None:
    mock_msal = MagicMock()
    mock_cache = MagicMock()
    mock_cache.has_state_changed = False
    mock_app = MagicMock()
    mock_app.get_accounts.return_value = [{"username": "user@test.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "cached-token"}
    mock_msal.SerializableTokenCache.return_value = mock_cache
    mock_msal.PublicClientApplication.return_value = mock_app

    with patch.dict("sys.modules", {"msal": mock_msal}):
        auth = MS365Auth.__new__(MS365Auth)
        auth._client_id = "test-id"
        auth._tenant_id = "common"
        auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
        auth._scopes = ["User.Read"]
        auth._app = mock_app
        auth._cache = mock_cache
        auth._pending_flow_message = ""

        token = await auth.get_token()
        assert token == "cached-token"
