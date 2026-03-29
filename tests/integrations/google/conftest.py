"""
Shared fixtures for Google Workspace integration tests.

All tests mock ``googleapiclient.discovery.build`` so no real Google APIs
are hit.  Each service returns a structured Mock object whose call chain
mirrors the real API (e.g. svc.users().messages().list(...).execute()).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.integrations.google.auth import GoogleAuth
from pincer.integrations.google.service_factory import GoogleServiceFactory


# ── Auth & factory fixtures ───────────────────────────────────────────────────


@pytest.fixture
def mock_credentials(tmp_path):
    """A fake credentials JSON file + token file for GoogleAuth."""
    import json
    import os

    creds_path = tmp_path / "google_credentials.json"
    token_path = tmp_path / "google_workspace_token.json"

    # Minimal OAuth client secrets format
    creds_path.write_text(
        json.dumps({
            "installed": {
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        })
    )
    from pincer.integrations.google.auth import ALL_SCOPES

    # Minimal saved token — includes all required scopes so the eager scope
    # check in get_credentials() passes and individual tests can exercise the
    # token-validity / refresh branches.
    token_path.write_text(
        json.dumps({
            "token": "ya29.fake_access_token",
            "refresh_token": "1//fake_refresh_token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "scopes": ALL_SCOPES,
            "expiry": "2099-01-01T00:00:00Z",
        })
    )
    os.chmod(token_path, 0o600)
    return creds_path, token_path


@pytest.fixture
def google_auth(mock_credentials):
    """GoogleAuth instance backed by mock credential files."""
    creds_path, token_path = mock_credentials
    return GoogleAuth(
        credentials_path=str(creds_path),
        token_path=str(token_path),
    )


def _build_mock_service():
    """Return a MagicMock with deep call chains for Google API services."""
    svc = MagicMock()
    # Make every chained call return a fresh Mock with an execute() method.
    # This covers svc.users().messages().list(...).execute() etc.
    svc.return_value = svc
    return svc


@pytest.fixture
def mock_factory():
    """GoogleServiceFactory where every get() returns a MagicMock service."""
    auth = MagicMock(spec=GoogleAuth)
    factory = GoogleServiceFactory(auth)

    services: dict[str, MagicMock] = {}

    async def _get(service_name: str):
        if service_name not in services:
            services[service_name] = _build_mock_service()
        return services[service_name]

    factory.get = _get
    factory._services_dict = services  # expose for test inspection
    return factory


# ── Per-service convenience fixtures ─────────────────────────────────────────


@pytest.fixture
def mock_gmail_service(mock_factory):
    """Pre-built mock Gmail service; also wires factory.get('gmail')."""
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["gmail"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_calendar_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["calendar"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_drive_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["drive"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_sheets_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["sheets"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_docs_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["docs"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_slides_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["slides"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_tasks_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["tasks"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_contacts_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["contacts"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


@pytest.fixture
def mock_meet_service(mock_factory):
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["meet"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc
