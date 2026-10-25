"""
Tests for GoogleAuth — token loading, refresh, and scope building.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from pincer.integrations.google.auth import ALL_SCOPES, SERVICE_SCOPES, GoogleAuth, scopes_for_services

# ── scopes_for_services ───────────────────────────────────────────────────────


def test_scopes_for_services_none_returns_all():
    scopes = scopes_for_services(None)
    assert scopes == ALL_SCOPES


def test_scopes_for_services_single():
    scopes = scopes_for_services(["gmail"])
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes
    assert "https://www.googleapis.com/auth/gmail.send" in scopes
    assert "https://www.googleapis.com/auth/calendar" not in scopes


def test_scopes_for_services_multiple():
    scopes = scopes_for_services(["gmail", "calendar"])
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes
    assert "https://www.googleapis.com/auth/calendar" in scopes


def test_scopes_for_services_no_duplicates():
    scopes = scopes_for_services(["gmail", "gmail"])
    assert scopes.count("https://www.googleapis.com/auth/gmail.modify") == 1


def test_scopes_for_services_unknown_service():
    # Unknown services are silently ignored
    scopes = scopes_for_services(["nonexistent"])
    assert scopes == []


# ── GoogleAuth.get_credentials ────────────────────────────────────────────────


def test_get_credentials_missing_credentials_file(tmp_path):
    auth = GoogleAuth(
        credentials_path=str(tmp_path / "missing.json"),
        token_path=str(tmp_path / "token.json"),
    )
    with pytest.raises(FileNotFoundError, match="setup"):
        auth.get_credentials()


def test_get_credentials_missing_token_file(tmp_path, mock_credentials):
    creds_path, _ = mock_credentials
    auth = GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(tmp_path / "no_token.json"),
    )
    with pytest.raises(FileNotFoundError, match="setup"):
        auth.get_credentials()


def test_get_credentials_valid_token(mock_credentials):
    creds_path, token_path = mock_credentials
    auth = GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(token_path),
    )
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False
    mock_creds.refresh_token = "refresh"

    # Credentials and Request are lazily imported inside get_credentials()
    with patch("google.oauth2.credentials.Credentials") as mock_creds_cls:
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds
        with patch("google.auth.transport.requests.Request"):
            creds = auth.get_credentials()
    assert creds is mock_creds


def test_get_credentials_refreshes_expired_token(mock_credentials):
    creds_path, token_path = mock_credentials
    auth = GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(token_path),
    )
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh"
    mock_creds.to_json.return_value = '{"token": "new"}'

    with patch("google.oauth2.credentials.Credentials") as mock_creds_cls:
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds
        with patch("google.auth.transport.requests.Request"):
            mock_creds.refresh.return_value = None
            auth.get_credentials()

    mock_creds.refresh.assert_called_once()


def test_get_credentials_no_refresh_token_raises(mock_credentials):
    creds_path, token_path = mock_credentials
    auth = GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(token_path),
    )
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = False
    mock_creds.refresh_token = None

    with patch("google.oauth2.credentials.Credentials") as mock_creds_cls:
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds
        with patch("google.auth.transport.requests.Request"), pytest.raises(RuntimeError, match="invalid"):
            auth.get_credentials()


def test_save_token_sets_permissions(mock_credentials, tmp_path):
    creds_path, _ = mock_credentials
    token_path = tmp_path / "new_token.json"
    auth = GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(token_path),
    )
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "abc"}'
    auth._credentials = mock_creds
    auth._save_token()

    assert token_path.exists()
    assert (os.stat(token_path).st_mode & 0o777) == 0o600


def test_token_path_property(mock_credentials):
    creds_path, token_path = mock_credentials
    auth = GoogleAuth(credentials_path=str(creds_path), token_path=str(token_path))
    assert auth.token_path == token_path


def test_credentials_path_property(mock_credentials):
    creds_path, token_path = mock_credentials
    auth = GoogleAuth(credentials_path=str(creds_path), token_path=str(token_path))
    assert auth.credentials_path == creds_path


# ── GoogleAuth.granted_services ───────────────────────────────────────────────


def _auth_with_token_scopes(tmp_path, scopes):
    import json

    token_path = tmp_path / "token.json"
    data = {"token": "ya29.fake"}
    if scopes is not None:
        data["scopes"] = scopes
    token_path.write_text(json.dumps(data))
    return GoogleAuth(credentials_path=str(tmp_path / "creds.json"), token_path=str(token_path))


def test_granted_services_calendar_only_token(tmp_path):
    auth = _auth_with_token_scopes(tmp_path, ["https://www.googleapis.com/auth/calendar"])
    assert auth.granted_services() == ["calendar"]


def test_granted_services_full_token(tmp_path):
    auth = _auth_with_token_scopes(tmp_path, ALL_SCOPES)
    assert auth.granted_services() == list(SERVICE_SCOPES)


def test_granted_services_token_missing_meet(tmp_path):
    scopes = [s for s in ALL_SCOPES if "meetings.space" not in s]
    auth = _auth_with_token_scopes(tmp_path, scopes)
    assert auth.granted_services() == [s for s in SERVICE_SCOPES if s != "meet"]


def test_granted_services_partial_meet_scopes_not_granted(tmp_path):
    scopes = [s for s in ALL_SCOPES if not s.endswith("meetings.space.settings")]
    auth = _auth_with_token_scopes(tmp_path, scopes)
    assert "meet" not in auth.granted_services()


def test_granted_services_token_without_scopes_field(tmp_path):
    auth = _auth_with_token_scopes(tmp_path, None)
    assert auth.granted_services() == list(SERVICE_SCOPES)


def test_services_property_defaults_to_all(tmp_path):
    auth = GoogleAuth(credentials_path=str(tmp_path / "c.json"), token_path=str(tmp_path / "t.json"))
    assert auth.services == list(SERVICE_SCOPES)


def test_services_property_respects_subset(tmp_path):
    auth = GoogleAuth(
        credentials_path=str(tmp_path / "c.json"), token_path=str(tmp_path / "t.json"), services=["gmail"]
    )
    assert auth.services == ["gmail"]
