"""
Slack integration for Pincer.

Provides 71 tools for managing Slack workspaces: channels, messages,
threads, files, reactions, pins, bookmarks, reminders, DND, search,
and user management using the official slack-sdk library.

Quick start::

    from pincer.integrations.slack import get_slack_client, register_all_tools
    from pincer.tools.registry import ToolRegistry

    client = get_slack_client()   # None if not configured
    if client:
        registry = ToolRegistry()
        count = register_all_tools(registry, client)

Setup::

    pincer setup-slack   # interactive token setup
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry


def get_slack_client() -> SlackClient | None:
    """Return a SlackClient if tokens are configured, else None."""
    import logging

    from pincer.integrations.slack.auth import load_tokens
    from pincer.integrations.slack.client import SlackClient

    _log = logging.getLogger(__name__)

    tokens = load_tokens()
    if not tokens.bot_token:
        return None

    try:
        return SlackClient(bot_token=tokens.bot_token, user_token=tokens.user_token)
    except Exception as exc:
        _log.warning("Slack client failed to initialize: %s", exc)
        return None


def register_all_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 71 Slack tools in *registry*. Returns count."""
    from pincer.integrations.slack.tools_channels import register_channel_tools
    from pincer.integrations.slack.tools_files import register_file_tools
    from pincer.integrations.slack.tools_messages import register_message_tools
    from pincer.integrations.slack.tools_misc import register_misc_tools
    from pincer.integrations.slack.tools_reactions import register_reaction_tools
    from pincer.integrations.slack.tools_users import register_user_tools

    count = 0
    count += register_channel_tools(registry, client)
    count += register_message_tools(registry, client)
    count += register_file_tools(registry, client)
    count += register_user_tools(registry, client)
    count += register_reaction_tools(registry, client)
    count += register_misc_tools(registry, client)
    return count


__all__ = ["get_slack_client", "register_all_tools"]
