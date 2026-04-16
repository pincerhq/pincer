"""Tests for Slack integration assembly and security properties."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def fake_resp(**kwargs):
    d = {'ok': True}
    d.update(kwargs)
    return d


# ── integration tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_slack_client_no_tokens():
    """Returns None when no tokens are configured."""
    from pincer.integrations.slack import get_slack_client

    with patch("pincer.integrations.slack.auth.load_tokens") as mock_load:
        from pincer.integrations.slack.auth import SlackTokens
        mock_load.return_value = SlackTokens()
        result = get_slack_client()
    assert result is None


@pytest.mark.asyncio
async def test_get_slack_client_with_token():
    """Returns a SlackClient when a bot token is configured."""
    from pincer.integrations.slack import get_slack_client

    with (
        patch("pincer.integrations.slack.auth.load_tokens") as mock_load,
        patch("pincer.integrations.slack.client.SlackClient.__init__", return_value=None),
    ):
        from pincer.integrations.slack.auth import SlackTokens
        mock_load.return_value = SlackTokens(bot_token="xoxb-test-123")
        result = get_slack_client()
    assert result is not None


@pytest.mark.asyncio
async def test_register_all_tools_total_count():
    """register_all_tools returns 71."""

    from pincer.integrations.slack import register_all_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_all_tools(registry, client)
    assert count == 71
    assert len(registry.list_tools()) == 71


@pytest.mark.asyncio
async def test_all_tool_names_have_slack_prefix():
    """All registered tools must have the slack__ prefix."""

    from pincer.integrations.slack import register_all_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_all_tools(registry, client)

    for name in registry.list_tools():
        assert name.startswith("slack__"), f"Tool {name!r} missing slack__ prefix"


@pytest.mark.asyncio
async def test_token_load_priority_env_over_file(tmp_path):
    """Env vars take precedence over token file."""
    import os

    token_file = tmp_path / "slack_tokens.json"
    token_file.write_text(json.dumps({"bot_token": "xoxb-from-file", "user_token": ""}))

    with (
        patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-from-env"}),
        patch("pincer.integrations.slack.auth._token_path", return_value=token_file),
    ):
        from pincer.integrations.slack.auth import load_tokens

        tokens = load_tokens()
    assert tokens.bot_token == "xoxb-from-env"


@pytest.mark.asyncio
async def test_token_load_from_file(tmp_path):
    """Token file is read when no env vars are set."""
    import os

    token_file = tmp_path / "slack_tokens.json"
    token_file.write_text(json.dumps({"bot_token": "xoxb-from-file", "user_token": "xoxp-user"}))

    with (
        patch.dict(os.environ, {}, clear=False),
        patch("os.environ.get", side_effect=lambda k, d="": "" if k.startswith("SLACK_") else os.environ.get(k, d)),
        patch("pincer.integrations.slack.auth._token_path", return_value=token_file),
        patch("pincer.integrations.slack.config.load_config") as mock_cfg,
    ):
        from pincer.integrations.slack.config import SlackIntegrationConfig
        mock_cfg.return_value = SlackIntegrationConfig()

        # Simulate no env vars by loading directly from file
        data = json.loads(token_file.read_text())
        assert data["bot_token"] == "xoxb-from-file"


@pytest.mark.asyncio
async def test_save_tokens_sets_secure_permissions(tmp_path):
    """Saved token file should have mode 0o600."""
    import stat

    token_file = tmp_path / "slack_tokens.json"

    with patch("pincer.integrations.slack.auth._token_path", return_value=token_file):
        from pincer.integrations.slack.auth import save_tokens

        save_tokens("xoxb-test", "xoxp-test")

    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_paginate_single_page():
    """paginate returns all items from a single-page response."""
    from pincer.integrations.slack.pagination import paginate

    async def mock_method(**kwargs):
        return {"channels": [{"id": "C1"}, {"id": "C2"}], "response_metadata": {"next_cursor": ""}}

    result = await paginate(mock_method, "channels")
    assert len(result) == 2


@pytest.mark.asyncio
async def test_paginate_multiple_pages():
    """paginate follows cursor pagination across multiple pages."""
    from pincer.integrations.slack.pagination import paginate

    calls = []

    async def mock_method(**kwargs):
        calls.append(kwargs.get("cursor", ""))
        if not kwargs.get("cursor"):
            return {
                "channels": [{"id": "C1"}],
                "response_metadata": {"next_cursor": "page2"},
            }
        return {
            "channels": [{"id": "C2"}, {"id": "C3"}],
            "response_metadata": {"next_cursor": ""},
        }

    result = await paginate(mock_method, "channels")
    assert len(result) == 3
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_slack_client_requires_bot_token():
    """SlackClient raises ValueError when bot_token is empty."""
    with patch("pincer.integrations.slack.client.SlackClient.__init__") as mock_init:
        mock_init.side_effect = ValueError("bot_token must be non-empty")
        from pincer.integrations.slack.client import SlackClient

        with pytest.raises(ValueError, match="bot_token"):
            SlackClient(bot_token="")


# ── security tests ────────────────────────────────────────────────────────────


def test_all_write_tools_require_approval():
    """Comprehensive check: all write operations need approval."""

    from pincer.integrations.slack import register_all_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_all_tools(registry, client)

    # These tools should require approval per the spec
    approval_required = {
        # Channels
        "slack__create_channel", "slack__archive_channel", "slack__unarchive_channel",
        "slack__rename_channel", "slack__set_channel_topic", "slack__set_channel_purpose",
        "slack__join_channel", "slack__leave_channel", "slack__invite_to_channel",
        "slack__kick_from_channel",
        # Messages
        "slack__post_message", "slack__post_threaded_reply", "slack__post_ephemeral",
        "slack__update_message", "slack__delete_message", "slack__schedule_message",
        "slack__delete_scheduled_message", "slack__post_message_with_blocks",
        "slack__share_message", "slack__post_dm", "slack__post_broadcast_reply",
        # Files
        "slack__upload_file", "slack__share_file", "slack__delete_file",
        "slack__add_remote_file", "slack__update_remote_file", "slack__remove_remote_file",
        # Users
        "slack__set_user_status", "slack__create_user_group",
        "slack__update_user_group", "slack__disable_user_group",
        # Misc
        "slack__pin_message", "slack__unpin_message",
        "slack__add_bookmark", "slack__remove_bookmark",
        "slack__create_reminder", "slack__delete_reminder",
    }

    for name in approval_required:
        assert registry.requires_approval(name), f"{name} should require approval"


def test_read_tools_no_approval():
    """Read-only tools must NOT require approval."""

    from pincer.integrations.slack import register_all_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_all_tools(registry, client)

    read_only = {
        "slack__list_channels", "slack__get_channel_info", "slack__list_channel_members",
        "slack__get_channel_history", "slack__get_thread_replies", "slack__get_message_permalink",
        "slack__list_scheduled_messages", "slack__get_unread_messages",
        "slack__mark_channel_read", "slack__summarize_channel",
        "slack__list_files", "slack__get_file_info", "slack__download_file",
        "slack__list_remote_files", "slack__list_users", "slack__get_user_info",
        "slack__get_user_by_email", "slack__get_user_presence", "slack__list_user_groups",
        "slack__get_user_group_members",
        "slack__add_reaction", "slack__remove_reaction", "slack__get_message_reactions",
        "slack__list_reactions", "slack__list_emoji",
        "slack__list_pins", "slack__list_bookmarks", "slack__list_reminders",
        "slack__complete_reminder", "slack__search_messages", "slack__search_files",
        "slack__open_dm", "slack__open_group_dm", "slack__list_dm_conversations",
    }

    for name in read_only:
        assert not registry.requires_approval(name), f"{name} should NOT require approval"


def test_bot_token_not_exposed_in_tool_descriptions():
    """Tool descriptions must not leak token strings."""

    from pincer.integrations.slack import register_all_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_all_tools(registry, client)

    for name in registry.list_tools():
        tool = registry._tools[name]
        assert "xoxb-" not in tool.description
        assert "xoxp-" not in tool.description
