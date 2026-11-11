"""
Shared fixtures for Google Workspace integration tests.

All tests mock ``googleapiclient.discovery.build`` so no real Google APIs
are hit.  Each service returns a structured Mock object whose call chain
mirrors the real API (e.g. svc.users().messages().list(...).execute()).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.auth import SERVICE_SCOPES, GoogleAuth
from pincer.integrations.google.service_factory import GoogleServiceFactory
from pincer.integrations.google.tools_calendar import register_calendar_tools
from pincer.tools.registry import ToolRegistry

# ── Workspace ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def workspace_dir(tmp_path_factory, monkeypatch):
    """The Drive / Gmail tools keep files in a local workspace of their own
    (never the developer's storage root, and outside tmp_path, which some tests
    expect to stay empty); returns its directory."""
    from pincer.storage.workspace import Workspace

    root = tmp_path_factory.mktemp("storage") / "workspace"
    ws = Workspace(root, root)
    ws.ensure()
    monkeypatch.setattr("pincer.integrations.google.workspace_files._workspace", lambda: ws)
    return root


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
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id.apps.googleusercontent.com",
                    "client_secret": "test-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    from pincer.integrations.google.auth import ALL_SCOPES

    # Minimal saved token — includes all required scopes so the eager scope
    # check in get_credentials() passes and individual tests can exercise the
    # token-validity / refresh branches.
    token_path.write_text(
        json.dumps(
            {
                "token": "ya29.fake_access_token",
                "refresh_token": "1//fake_refresh_token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "scopes": ALL_SCOPES,
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
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
    auth.services = list(SERVICE_SCOPES)
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


@pytest.fixture(autouse=True)
def _no_contacts_search_waits(monkeypatch):
    """Skip the real warmup/retry sleeps of google__search_contacts."""
    from pincer.integrations.google import tools_contacts

    monkeypatch.setattr(tools_contacts, "_WARMUP_WAIT_SECONDS", 0)
    monkeypatch.setattr(tools_contacts, "_RETRY_WAIT_SECONDS", 0)


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


@pytest.fixture
def mock_meet_beta_service(mock_factory):
    """Mock for the Meet v2beta service (members, moderation, artifacts)."""
    svc = _build_mock_service()
    services = getattr(mock_factory, "_services_dict", {})
    services["meet_beta"] = svc

    async def _get(name):
        return services.get(name, _build_mock_service())

    mock_factory.get = _get
    return svc


# ── Stateful fake Calendar fixtures ──────────────────────────────────────────
# Each calendar test module defines its own ``fake`` (a FakeCalendarService
# with the calendars and zones it needs); these build on it.


@pytest.fixture
def berlin_deployment(monkeypatch):
    """PINCER_TIMEZONE=Europe/Berlin — the deployment zone, not the calendar's."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "pincer.config.get_settings_relaxed",
        lambda: SimpleNamespace(timezone="Europe/Berlin", voice_timezone="Europe/Berlin"),
    )


@pytest.fixture
def fake():
    """A fake Calendar with a Europe/Kiev primary calendar (Google's name for the
    owner's zone); modules needing other calendars override this fixture."""
    return FakeCalendarService({"primary": "Europe/Kiev"})


@pytest.fixture
def fake_factory(fake, berlin_deployment):
    """A GoogleServiceFactory stand-in serving the module's fake Calendar service.

    Depends on ``berlin_deployment`` so no fake-calendar test can silently read
    the machine's real settings.
    """
    return FakeCalendarFactory(fake)


@pytest.fixture
def fake_registry(fake_factory):
    """A real ToolRegistry with the real calendar tools wired to the fake."""
    reg = ToolRegistry()
    register_calendar_tools(reg, fake_factory)
    return reg


@pytest.fixture
def instant_backoff(monkeypatch):
    """The quota retry wrapper without its real back-off sleeps (503 retry tests)."""
    import asyncio
    from types import SimpleNamespace

    from pincer.integrations.google import quota

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(quota, "asyncio", SimpleNamespace(sleep=_no_sleep, to_thread=asyncio.to_thread))


@pytest.fixture
def google_token(monkeypatch, mock_credentials, fake, berlin_deployment):
    """A configured Google token whose Calendar API is the module's ``fake``.

    Goes through the real ``get_google_factory`` path (config, token location,
    scope probe, service factory); only ``googleapiclient.discovery.build`` is
    swapped. Returns a switch: set ``.config`` to change ``[integrations.google]``.
    """
    from types import SimpleNamespace

    from pincer.integrations.google.config import GoogleIntegrationConfig

    creds_path, token_path = mock_credentials
    location = {"credentials_path": str(creds_path), "token_path": str(token_path)}
    monkeypatch.setattr("pincer.integrations.google.token_source.locate_token", lambda _settings: location)

    def _build(api, version, **_kwargs):
        assert api == "calendar", api
        return fake

    monkeypatch.setattr("googleapiclient.discovery.build", _build)
    state = SimpleNamespace(config=GoogleIntegrationConfig())
    monkeypatch.setattr("pincer.integrations.google.config.load_config", lambda: state.config)
    return state
