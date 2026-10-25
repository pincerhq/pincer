"""
Tests for get_google_factory + register_all_tools — per-service enablement
driven by the scopes actually present in the cached token.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from pincer.integrations.google import get_google_factory, google_status, register_all_tools
from pincer.integrations.google.auth import ALL_SCOPES, SERVICE_SCOPES
from pincer.integrations.google.config import GoogleIntegrationConfig
from pincer.tools.registry import ToolRegistry

_CALENDAR = "https://www.googleapis.com/auth/calendar"
_NO_MEET = [s for s in ALL_SCOPES if "meetings.space" not in s]


def _write_files(oauth_dir, scopes, token_name="google_workspace_token.json"):
    (oauth_dir / "google_credentials.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test.apps.googleusercontent.com",
                    "client_secret": "secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    (oauth_dir / token_name).write_text(
        json.dumps(
            {
                "token": "ya29.fake",
                "refresh_token": "1//fake",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "secret",
                "scopes": scopes,
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
    )


@pytest.fixture
def env(tmp_path):
    """Point google_oauth_dir at tmp_path and let tests set the integration config."""
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    cfg = GoogleIntegrationConfig()
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.integrations.google.config.load_config", side_effect=lambda: cfg),
    ):
        yield tmp_path, cfg


def _names(registry: ToolRegistry) -> set[str]:
    return {s["name"] for s in registry.get_schemas()}


def _service_tool_names(factory, service: str) -> set[str]:
    registry = ToolRegistry()
    register_all_tools(registry, factory, services=[service])
    return _names(registry)


def _registered(factory) -> set[str]:
    registry = ToolRegistry()
    register_all_tools(registry, factory)
    return _names(registry)


def test_legacy_calendar_only_token_registers_calendar_tools(env, caplog):
    oauth_dir, _ = env
    _write_files(oauth_dir, [_CALENDAR], token_name="google_token.json")

    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google"):
        factory = get_google_factory()

    assert factory is not None
    assert factory.auth.services == ["calendar"]
    names = _registered(factory)
    assert names
    assert names == _service_tool_names(factory, "calendar")
    assert all(n.startswith("google__") for n in names)
    assert "gmail" in caplog.text and "meet" in caplog.text


def test_token_missing_meet_registers_everything_but_meet(env):
    oauth_dir, _ = env
    _write_files(oauth_dir, _NO_MEET)

    factory = get_google_factory()

    assert factory is not None
    assert "meet" not in factory.auth.services
    names = _registered(factory)
    meet = _service_tool_names(factory, "meet")
    assert meet
    assert not names & meet
    for svc in ("gmail", "calendar", "drive", "docs", "sheets", "slides", "tasks", "contacts"):
        assert _service_tool_names(factory, svc) <= names


def test_get_credentials_does_not_raise_for_skipped_services(env):
    oauth_dir, _ = env
    _write_files(oauth_dir, [_CALENDAR], token_name="google_token.json")
    factory = get_google_factory()
    assert factory is not None
    assert factory.auth.missing_scopes() == []


def test_full_token_registers_every_tool(env):
    oauth_dir, _ = env
    _write_files(oauth_dir, ALL_SCOPES)
    factory = get_google_factory()
    assert factory is not None
    assert len(_registered(factory)) == 113


def test_services_config_respected(env):
    oauth_dir, cfg = env
    _write_files(oauth_dir, ALL_SCOPES)
    cfg.services = ["gmail", "drive"]

    factory = get_google_factory()

    assert factory is not None
    assert factory.auth.services == ["gmail", "drive"]
    assert _registered(factory) == _service_tool_names(factory, "gmail") | _service_tool_names(factory, "drive")


def test_disabled_in_config_returns_none(env):
    oauth_dir, cfg = env
    _write_files(oauth_dir, ALL_SCOPES)
    cfg.enabled = False
    assert get_google_factory() is None
    assert google_status()["status"] == "disabled"


def test_token_covering_no_configured_service_returns_none(env):
    oauth_dir, cfg = env
    _write_files(oauth_dir, [_CALENDAR])
    cfg.services = ["gmail"]
    assert get_google_factory() is None
    assert google_status()["status"] == "disabled"


def test_google_status_partial_lists_missing_scopes(env):
    oauth_dir, _ = env
    _write_files(oauth_dir, _NO_MEET)
    status = google_status()
    assert status["status"] == "partial"
    assert status["missing_scopes"] == SERVICE_SCOPES["meet"]
    assert "meet" not in status["enabled_services"]


def test_google_status_active_with_full_token(env):
    oauth_dir, _ = env
    _write_files(oauth_dir, ALL_SCOPES)
    status = google_status()
    assert status == {"status": "active", "enabled_services": list(SERVICE_SCOPES), "missing_scopes": []}
