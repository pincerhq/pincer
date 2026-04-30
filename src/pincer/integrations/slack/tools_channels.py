"""Channel / conversation tools — 16 tools.

All functions are async, accept a SlackClient as first arg, and return str.
The register_channel_tools() helper wires them into Pincer's ToolRegistry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_channel(c: dict[str, Any]) -> str:
    name = c.get("name") or c.get("id", "?")
    cid = c.get("id", "")
    is_private = c.get("is_private", False)
    is_archived = c.get("is_archived", False)
    members = c.get("num_members", "?")
    suffix = " [private]" if is_private else ""
    suffix += " [archived]" if is_archived else ""
    return f"#{name} (ID: {cid}, members: {members}){suffix}"


# ── tool functions ─────────────────────────────────────────────────────────────


async def slack__list_channels(
    client: SlackClient,
    types: str = "public_channel,private_channel",
    exclude_archived: bool = False,
    limit: int = 200,
) -> str:
    """List all channels/conversations in the workspace."""
    try:
        from pincer.integrations.slack.pagination import paginate

        channels = await paginate(
            client.bot.conversations_list,
            "channels",
            limit=min(limit, 200),
            types=types,
            exclude_archived=exclude_archived,
        )
        if not channels:
            return "No channels found."
        lines = [_fmt_channel(c) for c in channels]
        return f"Found {len(lines)} channel(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing channels: {exc}"


async def slack__get_channel_info(client: SlackClient, channel: str) -> str:
    """Get details for a specific channel: topic, purpose, member count, created date."""
    try:
        resp = await client.bot.conversations_info(channel=channel, include_num_members=True)
        c = resp.get("channel", {})
        lines = [
            f"Name: #{c.get('name', '?')}",
            f"ID: {c.get('id', '?')}",
            f"Type: {'private' if c.get('is_private') else 'public'}",
            f"Members: {c.get('num_members', '?')}",
            f"Topic: {c.get('topic', {}).get('value', '(none)')}",
            f"Purpose: {c.get('purpose', {}).get('value', '(none)')}",
            f"Archived: {c.get('is_archived', False)}",
            f"Created: {c.get('created', '?')}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error getting channel info: {exc}"


async def slack__list_channel_members(client: SlackClient, channel: str, limit: int = 200) -> str:
    """List all member user IDs in a channel."""
    try:
        from pincer.integrations.slack.pagination import paginate

        members = await paginate(
            client.bot.conversations_members,
            "members",
            limit=min(limit, 200),
            channel=channel,
        )
        if not members:
            return f"No members found in channel {channel}."
        return f"Members of {channel} ({len(members)}):\n" + "\n".join(members)
    except Exception as exc:
        return f"Error listing channel members: {exc}"


async def slack__create_channel(client: SlackClient, name: str, is_private: bool = False) -> str:
    """Create a new public or private channel."""
    try:
        resp = await client.bot.conversations_create(name=name, is_private=is_private)
        c = resp.get("channel", {})
        kind = "private" if is_private else "public"
        return f"Created {kind} channel #{c.get('name')} (ID: {c.get('id')})"
    except Exception as exc:
        return f"Error creating channel: {exc}"


async def slack__archive_channel(client: SlackClient, channel: str) -> str:
    """Archive a channel (makes it read-only and hides it from most views)."""
    try:
        await client.bot.conversations_archive(channel=channel)
        return f"Channel {channel} archived successfully."
    except Exception as exc:
        return f"Error archiving channel: {exc}"


async def slack__unarchive_channel(client: SlackClient, channel: str) -> str:
    """Unarchive a previously archived channel."""
    try:
        await client.bot.conversations_unarchive(channel=channel)
        return f"Channel {channel} unarchived successfully."
    except Exception as exc:
        return f"Error unarchiving channel: {exc}"


async def slack__rename_channel(client: SlackClient, channel: str, name: str) -> str:
    """Rename a channel."""
    try:
        resp = await client.bot.conversations_rename(channel=channel, name=name)
        c = resp.get("channel", {})
        return f"Channel renamed to #{c.get('name')} (ID: {c.get('id')})"
    except Exception as exc:
        return f"Error renaming channel: {exc}"


async def slack__set_channel_topic(client: SlackClient, channel: str, topic: str) -> str:
    """Set the topic for a channel."""
    try:
        await client.bot.conversations_setTopic(channel=channel, topic=topic)
        return f"Topic for {channel} set to: {topic}"
    except Exception as exc:
        return f"Error setting channel topic: {exc}"


async def slack__set_channel_purpose(client: SlackClient, channel: str, purpose: str) -> str:
    """Set the purpose/description for a channel."""
    try:
        await client.bot.conversations_setPurpose(channel=channel, purpose=purpose)
        return f"Purpose for {channel} set to: {purpose}"
    except Exception as exc:
        return f"Error setting channel purpose: {exc}"


async def slack__join_channel(client: SlackClient, channel: str) -> str:
    """Join a public channel."""
    try:
        resp = await client.bot.conversations_join(channel=channel)
        c = resp.get("channel", {})
        return f"Joined channel #{c.get('name', channel)}"
    except Exception as exc:
        return f"Error joining channel: {exc}"


async def slack__leave_channel(client: SlackClient, channel: str) -> str:
    """Leave a channel the bot is currently in."""
    try:
        await client.bot.conversations_leave(channel=channel)
        return f"Left channel {channel}."
    except Exception as exc:
        return f"Error leaving channel: {exc}"


async def slack__invite_to_channel(client: SlackClient, channel: str, users: str) -> str:
    """Invite one or more users (comma-separated IDs) to a channel."""
    try:
        user_list = [u.strip() for u in users.split(",") if u.strip()]
        await client.bot.conversations_invite(channel=channel, users=user_list)
        return f"Invited {', '.join(user_list)} to channel {channel}."
    except Exception as exc:
        return f"Error inviting to channel: {exc}"


async def slack__kick_from_channel(client: SlackClient, channel: str, user: str) -> str:
    """Remove a user from a channel."""
    try:
        await client.bot.conversations_kick(channel=channel, user=user)
        return f"Removed user {user} from channel {channel}."
    except Exception as exc:
        return f"Error removing user from channel: {exc}"


async def slack__open_dm(client: SlackClient, user: str) -> str:
    """Open a direct message channel with a user (does not send a message)."""
    try:
        resp = await client.bot.conversations_open(users=[user])
        c = resp.get("channel", {})
        return f"DM channel opened: {c.get('id', '?')}"
    except Exception as exc:
        return f"Error opening DM: {exc}"


async def slack__open_group_dm(client: SlackClient, users: str) -> str:
    """Open a multi-person DM (MPIM) with comma-separated user IDs."""
    try:
        user_list = [u.strip() for u in users.split(",") if u.strip()]
        resp = await client.bot.conversations_open(users=user_list)
        c = resp.get("channel", {})
        return f"Group DM opened: {c.get('id', '?')} with {', '.join(user_list)}"
    except Exception as exc:
        return f"Error opening group DM: {exc}"


async def slack__list_dm_conversations(client: SlackClient, limit: int = 100) -> str:
    """List all open DM (im) and multi-person DM (mpim) conversations."""
    try:
        from pincer.integrations.slack.pagination import paginate

        dms = await paginate(
            client.bot.conversations_list,
            "channels",
            limit=min(limit, 200),
            types="im,mpim",
        )
        if not dms:
            return "No DM conversations found."
        lines = []
        for dm in dms:
            cid = dm.get("id", "?")
            user = dm.get("user", "")
            kind = "MPIM" if dm.get("is_mpim") else "DM"
            lines.append(f"{kind} {cid}" + (f" with {user}" if user else ""))
        return f"Found {len(lines)} DM conversation(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing DM conversations: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_channel_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 16 channel tools. Returns 16."""

    def _h(
        fn: Callable[..., Awaitable[str]],
    ) -> Callable[..., Awaitable[str]]:
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__list_channels",
        description=(
            "List all channels in the Slack workspace. Use types to filter: public_channel, private_channel, im, mpim."
        ),
        handler=_h(slack__list_channels),
        parameters={
            "type": "object",
            "properties": {
                "types": {
                    "type": "string",
                    "description": "Comma-separated channel types (default: public_channel,private_channel)",
                    "default": "public_channel,private_channel",
                },
                "exclude_archived": {
                    "type": "boolean",
                    "description": "Exclude archived channels",
                    "default": False,
                },
                "limit": {"type": "integer", "description": "Max channels to return", "default": 200},
            },
            "required": [],
        },
    )
    registry.register(
        name="slack__get_channel_info",
        description="Get channel details: topic, purpose, member count, created date.",
        handler=_h(slack__get_channel_info),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID (e.g. C1234567)"}},
            "required": ["channel"],
        },
    )
    registry.register(
        name="slack__list_channel_members",
        description="List all member user IDs in a channel.",
        handler=_h(slack__list_channel_members),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["channel"],
        },
    )
    registry.register(
        name="slack__create_channel",
        description="Create a new public or private Slack channel.",
        handler=_h(slack__create_channel),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Channel name (lowercase, no spaces)"},
                "is_private": {"type": "boolean", "description": "Create as private channel", "default": False},
            },
            "required": ["name"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__archive_channel",
        description="Archive a channel (makes it read-only).",
        handler=_h(slack__archive_channel),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID to archive"}},
            "required": ["channel"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__unarchive_channel",
        description="Unarchive a previously archived channel.",
        handler=_h(slack__unarchive_channel),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID to unarchive"}},
            "required": ["channel"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__rename_channel",
        description="Rename a Slack channel.",
        handler=_h(slack__rename_channel),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "name": {"type": "string", "description": "New channel name"},
            },
            "required": ["channel", "name"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__set_channel_topic",
        description="Set the topic for a channel.",
        handler=_h(slack__set_channel_topic),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "topic": {"type": "string", "description": "New topic text"},
            },
            "required": ["channel", "topic"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__set_channel_purpose",
        description="Set the purpose/description for a channel.",
        handler=_h(slack__set_channel_purpose),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "purpose": {"type": "string", "description": "New purpose text"},
            },
            "required": ["channel", "purpose"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__join_channel",
        description="Join a public Slack channel.",
        handler=_h(slack__join_channel),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID or name"}},
            "required": ["channel"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__leave_channel",
        description="Leave a Slack channel.",
        handler=_h(slack__leave_channel),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID"}},
            "required": ["channel"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__invite_to_channel",
        description="Invite one or more users to a channel.",
        handler=_h(slack__invite_to_channel),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "users": {"type": "string", "description": "Comma-separated user IDs (e.g. U123,U456)"},
            },
            "required": ["channel", "users"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__kick_from_channel",
        description="Remove a user from a channel.",
        handler=_h(slack__kick_from_channel),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "user": {"type": "string", "description": "User ID to remove"},
            },
            "required": ["channel", "user"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__open_dm",
        description="Open a direct message channel with a user (does not send a message).",
        handler=_h(slack__open_dm),
        parameters={
            "type": "object",
            "properties": {"user": {"type": "string", "description": "User ID (e.g. U1234567)"}},
            "required": ["user"],
        },
    )
    registry.register(
        name="slack__open_group_dm",
        description="Open a multi-person DM with comma-separated user IDs.",
        handler=_h(slack__open_group_dm),
        parameters={
            "type": "object",
            "properties": {"users": {"type": "string", "description": "Comma-separated user IDs"}},
            "required": ["users"],
        },
    )
    registry.register(
        name="slack__list_dm_conversations",
        description="List all open DM and multi-person DM conversations.",
        handler=_h(slack__list_dm_conversations),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
            "required": [],
        },
    )

    return 16
