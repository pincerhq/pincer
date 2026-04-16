"""Reaction / emoji tools — 5 tools.

All functions are async, accept a SlackClient as first arg, and return str.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── tool functions ─────────────────────────────────────────────────────────────


async def slack__add_reaction(
    client: SlackClient, channel: str, timestamp: str, name: str
) -> str:
    """Add an emoji reaction to a message.

    name: emoji name without colons (e.g. 'thumbsup').
    """
    try:
        await client.bot.reactions_add(channel=channel, timestamp=timestamp, name=name)
        return f"Reaction :{name}: added to message {timestamp} in {channel}."
    except Exception as exc:
        return f"Error adding reaction: {exc}"


async def slack__remove_reaction(
    client: SlackClient, channel: str, timestamp: str, name: str
) -> str:
    """Remove an emoji reaction from a message."""
    try:
        await client.bot.reactions_remove(channel=channel, timestamp=timestamp, name=name)
        return f"Reaction :{name}: removed from message {timestamp} in {channel}."
    except Exception as exc:
        return f"Error removing reaction: {exc}"


async def slack__get_message_reactions(
    client: SlackClient, channel: str, timestamp: str
) -> str:
    """Get all emoji reactions on a specific message."""
    try:
        resp = await client.bot.reactions_get(channel=channel, timestamp=timestamp)
        item = resp.get("message", resp.get("item", {}))
        reactions = item.get("reactions", [])
        if not reactions:
            return f"No reactions on message {timestamp}."
        lines = [
            f":{r.get('name', '?')}: × {r.get('count', 0)} (users: {', '.join(r.get('users', [])[:5])})"
            for r in reactions
        ]
        return f"Reactions on {timestamp}:\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error getting reactions: {exc}"


async def slack__list_reactions(
    client: SlackClient, user: str = "", full: bool = False
) -> str:
    """List items the calling user (or specified user) has reacted to."""
    try:
        kwargs: dict[str, Any] = {"limit": 50, "full": full}
        if user:
            kwargs["user"] = user
        resp = await client.bot.reactions_list(**kwargs)
        items = resp.get("items", [])
        if not items:
            return "No reactions found."
        lines = []
        for item in items:
            kind = item.get("type", "?")
            reactions = [
                f":{r.get('name')}:" for r in item.get("reactions", [])
            ]
            lines.append(f"{kind}: {', '.join(reactions)}")
        return f"Found {len(lines)} reacted item(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing reactions: {exc}"


async def slack__list_emoji(client: SlackClient) -> str:
    """List all custom emoji in the workspace."""
    try:
        resp = await client.bot.emoji_list()
        emoji = resp.get("emoji", {})
        if not emoji:
            return "No custom emoji found."
        names = sorted(emoji.keys())
        return f"Found {len(names)} custom emoji:\n" + ", ".join(f":{n}:" for n in names)
    except Exception as exc:
        return f"Error listing emoji: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_reaction_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 5 reaction tools. Returns 5."""

    def _h(fn: Any):  # type: ignore[return]
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__add_reaction",
        description="Add an emoji reaction to a Slack message.",
        handler=_h(slack__add_reaction),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
                "name": {"type": "string", "description": "Emoji name without colons (e.g. thumbsup)"},
            },
            "required": ["channel", "timestamp", "name"],
        },
    )
    registry.register(
        name="slack__remove_reaction",
        description="Remove an emoji reaction from a Slack message.",
        handler=_h(slack__remove_reaction),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
                "name": {"type": "string", "description": "Emoji name without colons"},
            },
            "required": ["channel", "timestamp", "name"],
        },
    )
    registry.register(
        name="slack__get_message_reactions",
        description="Get all emoji reactions and their counts on a specific message.",
        handler=_h(slack__get_message_reactions),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
            },
            "required": ["channel", "timestamp"],
        },
    )
    registry.register(
        name="slack__list_reactions",
        description="List items that the calling user has reacted to.",
        handler=_h(slack__list_reactions),
        parameters={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "User ID (optional, defaults to bot user)", "default": ""},
                "full": {"type": "boolean", "description": "Include full item objects", "default": False},
            },
            "required": [],
        },
    )
    registry.register(
        name="slack__list_emoji",
        description="List all custom emoji available in the workspace.",
        handler=_h(slack__list_emoji),
        parameters={"type": "object", "properties": {}, "required": []},
    )

    return 5
