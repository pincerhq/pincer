"""
Tests for Google Workspace tool registration and server setup.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pincer.integrations.google import get_google_factory, register_all_tools
from pincer.tools.registry import ToolRegistry

# ── register_all_tools ────────────────────────────────────────────────────────


async def test_register_all_tools_returns_112(mock_factory):
    registry = ToolRegistry()
    count = register_all_tools(registry, mock_factory)
    assert count == 112


async def test_register_all_tools_google_prefix(mock_factory):
    registry = ToolRegistry()
    register_all_tools(registry, mock_factory)
    tool_names = list(registry._tools.keys())
    assert all(name.startswith("google__") for name in tool_names)


async def test_register_all_tools_write_tools_require_approval(mock_factory):
    registry = ToolRegistry()
    register_all_tools(registry, mock_factory)

    # All write tools should require approval
    write_tools = [
        "google__send_message",
        "google__create_event",
        "google__trash_file",
        "google__create_doc",
        "google__append_sheet_values",
        "google__create_presentation",
        "google__complete_task",
        "google__create_contact",
    ]
    for tool_name in write_tools:
        assert tool_name in registry._tools, f"Tool {tool_name} not registered"
        assert registry._tools[tool_name].require_approval, f"{tool_name} should require approval"


async def test_register_all_tools_read_tools_no_approval(mock_factory):
    registry = ToolRegistry()
    register_all_tools(registry, mock_factory)

    read_tools = [
        "google__list_labels",
        "google__search_messages",
        "google__get_message",
        "google__list_events",
        "google__get_event",
        "google__list_drive_files",
        "google__get_sheet_values",
        "google__list_task_lists",
        "google__list_contacts",
    ]
    for tool_name in read_tools:
        assert tool_name in registry._tools, f"Tool {tool_name} not registered"
        assert not registry._tools[tool_name].require_approval, f"{tool_name} should NOT require approval"


async def test_register_gmail_tool_count(mock_factory):
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    registry = ToolRegistry()
    count = register_gmail_tools(registry, mock_factory)
    assert count == 19


async def test_register_calendar_tool_count(mock_factory):
    from pincer.integrations.google.tools_calendar import register_calendar_tools
    registry = ToolRegistry()
    count = register_calendar_tools(registry, mock_factory)
    assert count == 12


async def test_register_drive_tool_count(mock_factory):
    from pincer.integrations.google.tools_drive import register_drive_tools
    registry = ToolRegistry()
    count = register_drive_tools(registry, mock_factory)
    assert count == 15


# ── get_google_factory ────────────────────────────────────────────────────────


def test_get_google_factory_returns_none_when_unconfigured(tmp_path):
    """When no credentials file exists, factory is None."""
    with patch("pincer.config.get_settings_relaxed") as mock_settings:
        settings = MagicMock()
        settings.google_oauth_dir.return_value = tmp_path  # empty dir → no files
        mock_settings.return_value = settings

        factory = get_google_factory()
        assert factory is None


def test_get_google_factory_returns_factory_when_configured(tmp_path):
    """When credential and token files exist, returns a factory."""
    import json

    creds_path = tmp_path / "google_credentials.json"
    token_path = tmp_path / "google_workspace_token.json"

    creds_path.write_text(
        json.dumps({
            "installed": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        })
    )
    token_path.write_text(
        json.dumps({
            "token": "ya29.fake",
            "refresh_token": "1//fake",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": [],
            "expiry": "2099-01-01T00:00:00Z",
        })
    )

    with patch("pincer.config.get_settings_relaxed") as mock_settings:
        settings = MagicMock()
        settings.google_oauth_dir.return_value = tmp_path
        mock_settings.return_value = settings

        factory = get_google_factory()
        assert factory is not None
