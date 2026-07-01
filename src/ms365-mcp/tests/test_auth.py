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
