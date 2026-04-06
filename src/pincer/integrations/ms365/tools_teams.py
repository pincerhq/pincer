"""
Microsoft Teams messaging tools — 7 tools for team and chat messaging.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.ms365.graph_client import GraphClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


async def teams__list_teams(client: GraphClient) -> str:
    """List user's teams."""
    data = await client.get("/me/joinedTeams")
    teams = data.get("value", [])
    if not teams:
        return "No teams found."
    lines = [f"  {t.get('displayName', '?')} — {t.get('description', '')}\n    ID: {t.get('id', '')}" for t in teams]
    return f"{len(lines)} team(s):\n" + "\n\n".join(lines)


async def teams__list_channels(
    client: GraphClient,
    team_id: str,
) -> str:
    """List channels in a team."""
    data = await client.get(f"/teams/{team_id}/channels")
    channels = data.get("value", [])
    if not channels:
        return "No channels found."
    lines = []
    for ch in channels:
        membership = ch.get("membershipType", "standard")
        lines.append(f"  {ch.get('displayName', '?')} ({membership})\n    ID: {ch.get('id', '')}")
    return f"{len(lines)} channel(s):\n" + "\n\n".join(lines)


async def teams__list_channel_messages(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    max_results: int = 25,
) -> str:
    """List messages in a Teams channel."""
    data = await client.get(
        f"/teams/{team_id}/channels/{channel_id}/messages",
        params={"$top": str(max_results)},
    )
    messages = data.get("value", [])
    if not messages:
        return "No messages in this channel."
    lines = []
    for msg in messages:
        sender = msg.get("from", {}).get("user", {}).get("displayName", "?")
        body = msg.get("body", {}).get("content", "")
        # Strip HTML
        import re
        body_text = re.sub(r"<[^>]+>", "", body).strip()[:200]
        lines.append(
            f"  [{msg.get('createdDateTime', '')}] {sender}:\n"
            f"    {body_text}\n"
            f"    ID: {msg.get('id', '')}"
        )
    return f"{len(lines)} message(s):\n" + "\n\n".join(lines)


async def teams__get_channel_message(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    message_id: str,
) -> str:
    """Get a specific channel message."""
    msg = await client.get(f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}")
    sender = msg.get("from", {}).get("user", {}).get("displayName", "?")
    body = msg.get("body", {}).get("content", "")
    import re
    body_text = re.sub(r"<[^>]+>", "", body).strip()

    lines = [
        f"From: {sender}",
        f"Date: {msg.get('createdDateTime', '')}",
        f"Type: {msg.get('messageType', '')}",
        f"ID: {msg.get('id', '')}",
        "",
        body_text[:2000],
    ]
    attachments = msg.get("attachments", [])
    if attachments:
        lines.append(f"\nAttachments ({len(attachments)}):")
        for att in attachments:
            lines.append(f"  {att.get('name', '?')} ({att.get('contentType', '?')})")
    return "\n".join(lines)


async def teams__send_channel_message(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    body: str,
    body_type: str = "text",
) -> str:
    """Send a message to a Teams channel."""
    msg_body = {
        "body": {
            "contentType": "html" if body_type == "html" else "text",
            "content": body,
        }
    }
    result = await client.post(
        f"/teams/{team_id}/channels/{channel_id}/messages",
        json=msg_body,
    )
    return f"Message sent to channel (id={result.get('id', '')})"


async def teams__list_chats(
    client: GraphClient,
    max_results: int = 25,
) -> str:
    """List 1:1 and group chats."""
    data = await client.get("/me/chats", params={"$top": str(max_results), "$expand": "members"})
    chats = data.get("value", [])
    if not chats:
        return "No chats found."
    lines = []
    for chat in chats:
        chat_type = chat.get("chatType", "?")
        topic = chat.get("topic", "")
        members = chat.get("members", [])
        member_names = [m.get("displayName", "?") for m in members[:5]]
        display = topic or ", ".join(member_names)
        lines.append(f"  [{chat_type}] {display}\n    ID: {chat.get('id', '')}")
    return f"{len(lines)} chat(s):\n" + "\n\n".join(lines)


async def teams__send_chat_message(
    client: GraphClient,
    chat_id: str,
    body: str,
    body_type: str = "text",
) -> str:
    """Send a 1:1 or group chat message."""
    msg_body = {
        "body": {
            "contentType": "html" if body_type == "html" else "text",
            "content": body,
        }
    }
    result = await client.post(f"/chats/{chat_id}/messages", json=msg_body)
    return f"Chat message sent (id={result.get('id', '')})"


# ── Registry ��──────────────────────────��──────────────────────────────────────


def register_teams_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 7 Teams tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return await fn(client, **kwargs)
        return wrapper

    registry.register(
        name="teams__list_teams",
        description="List Microsoft Teams the user has joined.",
        handler=_h(teams__list_teams),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="teams__list_channels",
        description="List channels in a Microsoft Teams team.",
        handler=_h(teams__list_channels),
        parameters={
            "type": "object",
            "properties": {"team_id": {"type": "string"}},
            "required": ["team_id"],
        },
    )
    registry.register(
        name="teams__list_channel_messages",
        description="List recent messages in a Teams channel.",
        handler=_h(teams__list_channel_messages),
        parameters={
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "max_results": {"type": "integer", "default": 25},
            },
            "required": ["team_id", "channel_id"],
        },
    )
    registry.register(
        name="teams__get_channel_message",
        description="Get a specific message from a Teams channel.",
        handler=_h(teams__get_channel_message),
        parameters={
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["team_id", "channel_id", "message_id"],
        },
    )
    registry.register(
        name="teams__send_channel_message",
        description="Send a message to a Microsoft Teams channel.",
        handler=_h(teams__send_channel_message),
        parameters={
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "body": {"type": "string"},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["team_id", "channel_id", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="teams__list_chats",
        description="List 1:1 and group chats in Microsoft Teams.",
        handler=_h(teams__list_chats),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 25}},
            "required": [],
        },
    )
    registry.register(
        name="teams__send_chat_message",
        description="Send a 1:1 or group chat message in Microsoft Teams.",
        handler=_h(teams__send_chat_message),
        parameters={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "body": {"type": "string"},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["chat_id", "body"],
        },
        require_approval=True,
    )
    return 7
