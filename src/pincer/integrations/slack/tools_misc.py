"""Miscellaneous tools — 12 tools: pins, bookmarks, reminders, DND, search.

All functions are async, accept a SlackClient as first arg, and return str.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── pins ───────────────────────────────────────────────────────────────────────


async def slack__pin_message(
    client: SlackClient, channel: str, timestamp: str
) -> str:
    """Pin a message to a channel."""
    try:
        await client.bot.pins_add(channel=channel, timestamp=timestamp)
        return f"Message {timestamp} pinned to {channel}."
    except Exception as exc:
        return f"Error pinning message: {exc}"


async def slack__unpin_message(
    client: SlackClient, channel: str, timestamp: str
) -> str:
    """Unpin a message from a channel."""
    try:
        await client.bot.pins_remove(channel=channel, timestamp=timestamp)
        return f"Message {timestamp} unpinned from {channel}."
    except Exception as exc:
        return f"Error unpinning message: {exc}"


async def slack__list_pins(client: SlackClient, channel: str) -> str:
    """List all pinned items in a channel."""
    try:
        resp = await client.bot.pins_list(channel=channel)
        items = resp.get("items", [])
        if not items:
            return f"No pinned items in {channel}."
        lines = []
        for item in items:
            kind = item.get("type", "?")
            msg = item.get("message", {})
            ts = msg.get("ts", "?")
            text = str(msg.get("text", ""))[:100]
            lines.append(f"[{kind}] ts={ts}: {text}")
        return f"Pinned items in {channel} ({len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing pins: {exc}"


# ── bookmarks ─────────────────────────────────────────────────────────────────


async def slack__add_bookmark(
    client: SlackClient,
    channel_id: str,
    title: str,
    link: str,
    emoji: str = "",
) -> str:
    """Add a bookmark (link) to a channel."""
    try:
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "title": title,
            "type": "link",
            "link": link,
        }
        if emoji:
            kwargs["emoji"] = emoji
        resp = await client.bot.bookmarks_add(**kwargs)
        bm = resp.get("bookmark", {})
        return f"Bookmark added: '{bm.get('title', title)}' (ID: {bm.get('id', '?')}) to {channel_id}"
    except Exception as exc:
        return f"Error adding bookmark: {exc}"


async def slack__list_bookmarks(client: SlackClient, channel_id: str) -> str:
    """List all bookmarks in a channel."""
    try:
        resp = await client.bot.bookmarks_list(channel_id=channel_id)
        bookmarks = resp.get("bookmarks", [])
        if not bookmarks:
            return f"No bookmarks in {channel_id}."
        lines = [
            f"'{bm.get('title', '?')}' — {bm.get('link', '')} (ID: {bm.get('id', '?')})"
            for bm in bookmarks
        ]
        return f"Bookmarks in {channel_id} ({len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing bookmarks: {exc}"


async def slack__remove_bookmark(
    client: SlackClient, channel_id: str, bookmark_id: str
) -> str:
    """Remove a bookmark from a channel."""
    try:
        await client.bot.bookmarks_remove(channel_id=channel_id, bookmark_id=bookmark_id)
        return f"Bookmark {bookmark_id} removed from {channel_id}."
    except Exception as exc:
        return f"Error removing bookmark: {exc}"


# ── reminders ─────────────────────────────────────────────────────────────────


async def slack__create_reminder(
    client: SlackClient,
    text: str,
    time: str,
    user: str = "",
) -> str:
    """Create a reminder.

    time: Natural language time (e.g. 'tomorrow 9am') or Unix timestamp string.
    user: User ID to create reminder for (defaults to authenticated user).
    """
    try:
        kwargs: dict[str, Any] = {"text": text, "time": time}
        if user:
            kwargs["user"] = user
        resp = await client.bot.reminders_add(**kwargs)
        r = resp.get("reminder", {})
        return (
            f"Reminder created: '{r.get('text', text)}' "
            f"(ID: {r.get('id', '?')}, time: {r.get('time', time)})"
        )
    except Exception as exc:
        return f"Error creating reminder: {exc}"


async def slack__list_reminders(client: SlackClient) -> str:
    """List all reminders for the authenticated user."""
    try:
        resp = await client.bot.reminders_list()
        reminders = resp.get("reminders", [])
        if not reminders:
            return "No reminders found."
        lines = []
        for r in reminders:
            complete = " [complete]" if r.get("complete_ts") else ""
            lines.append(
                f"ID: {r.get('id', '?')} | time: {r.get('time', '?')} | "
                f"'{r.get('text', '')}'{complete}"
            )
        return f"Found {len(lines)} reminder(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing reminders: {exc}"


async def slack__complete_reminder(client: SlackClient, reminder: str) -> str:
    """Mark a reminder as complete."""
    try:
        await client.bot.reminders_complete(reminder=reminder)
        return f"Reminder {reminder} marked as complete."
    except Exception as exc:
        return f"Error completing reminder: {exc}"


async def slack__delete_reminder(client: SlackClient, reminder: str) -> str:
    """Delete a reminder."""
    try:
        await client.bot.reminders_delete(reminder=reminder)
        return f"Reminder {reminder} deleted."
    except Exception as exc:
        return f"Error deleting reminder: {exc}"


# ── search ────────────────────────────────────────────────────────────────────


async def slack__search_messages(
    client: SlackClient,
    query: str,
    sort: str = "score",
    count: int = 20,
) -> str:
    """Full-text search across workspace messages. Requires user token.

    sort: 'score' (relevance) or 'timestamp' (newest first).
    """
    try:
        resp = await client.user.search_messages(
            query=query,
            sort=sort,
            count=min(count, 100),
        )
        messages = resp.get("messages", {})
        matches = messages.get("matches", [])
        if not matches:
            return f"No messages found for query: {query}"
        lines = []
        for m in matches:
            channel = m.get("channel", {}).get("name", "?")
            ts = m.get("ts", "?")
            text = str(m.get("text", ""))[:200]
            lines.append(f"#{channel} [{ts}]: {text}")
        total = messages.get("total", len(matches))
        return f"Found {total} message(s) (showing {len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error searching messages: {exc}"


async def slack__search_files(
    client: SlackClient,
    query: str,
    sort: str = "score",
    count: int = 20,
) -> str:
    """Full-text search for files across the workspace. Requires user token."""
    try:
        resp = await client.user.search_files(
            query=query,
            sort=sort,
            count=min(count, 100),
        )
        files_data = resp.get("files", {})
        matches = files_data.get("matches", [])
        if not matches:
            return f"No files found for query: {query}"
        lines = []
        for f in matches:
            name = f.get("name", "?")
            fid = f.get("id", "?")
            kind = f.get("pretty_type", "?")
            lines.append(f"{name} (ID: {fid}, {kind})")
        total = files_data.get("total", len(matches))
        return f"Found {total} file(s) (showing {len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error searching files: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_misc_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 12 misc tools (pins, bookmarks, reminders, search). Returns 12."""

    def _h(fn: Any):  # type: ignore[return]
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__pin_message",
        description="Pin a message to a Slack channel.",
        handler=_h(slack__pin_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
            },
            "required": ["channel", "timestamp"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__unpin_message",
        description="Unpin a message from a Slack channel.",
        handler=_h(slack__unpin_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
            },
            "required": ["channel", "timestamp"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__list_pins",
        description="List all pinned items in a channel.",
        handler=_h(slack__list_pins),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel ID"}},
            "required": ["channel"],
        },
    )
    registry.register(
        name="slack__add_bookmark",
        description="Add a bookmark (link) to a Slack channel.",
        handler=_h(slack__add_bookmark),
        parameters={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Channel ID"},
                "title": {"type": "string", "description": "Bookmark title"},
                "link": {"type": "string", "description": "URL to bookmark"},
                "emoji": {"type": "string", "description": "Emoji icon (e.g. :link:)", "default": ""},
            },
            "required": ["channel_id", "title", "link"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__list_bookmarks",
        description="List all bookmarks in a channel.",
        handler=_h(slack__list_bookmarks),
        parameters={
            "type": "object",
            "properties": {"channel_id": {"type": "string", "description": "Channel ID"}},
            "required": ["channel_id"],
        },
    )
    registry.register(
        name="slack__remove_bookmark",
        description="Remove a bookmark from a channel.",
        handler=_h(slack__remove_bookmark),
        parameters={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Channel ID"},
                "bookmark_id": {"type": "string", "description": "Bookmark ID"},
            },
            "required": ["channel_id", "bookmark_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__create_reminder",
        description=(
            "Create a Slack reminder. "
            "time can be natural language ('tomorrow 9am') or a Unix timestamp."
        ),
        handler=_h(slack__create_reminder),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Reminder text"},
                "time": {"type": "string", "description": "When to remind (natural language or Unix timestamp)"},
                "user": {"type": "string", "description": "User ID to set reminder for (optional)", "default": ""},
            },
            "required": ["text", "time"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__list_reminders",
        description="List all reminders for the authenticated user.",
        handler=_h(slack__list_reminders),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="slack__complete_reminder",
        description="Mark a reminder as complete.",
        handler=_h(slack__complete_reminder),
        parameters={
            "type": "object",
            "properties": {"reminder": {"type": "string", "description": "Reminder ID (Rm...)"}},
            "required": ["reminder"],
        },
    )
    registry.register(
        name="slack__delete_reminder",
        description="Delete a Slack reminder.",
        handler=_h(slack__delete_reminder),
        parameters={
            "type": "object",
            "properties": {"reminder": {"type": "string", "description": "Reminder ID (Rm...)"}},
            "required": ["reminder"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__search_messages",
        description=(
            "Full-text search across all Slack messages. "
            "Requires a user token (SLACK_USER_TOKEN) for access."
        ),
        handler=_h(slack__search_messages),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (supports Slack modifiers like in:#channel)"},
                "sort": {"type": "string", "enum": ["score", "timestamp"], "default": "score"},
                "count": {"type": "integer", "description": "Max results to return", "default": 20},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="slack__search_files",
        description=(
            "Full-text search for files across the workspace. "
            "Requires a user token (SLACK_USER_TOKEN)."
        ),
        handler=_h(slack__search_files),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "sort": {"type": "string", "enum": ["score", "timestamp"], "default": "score"},
                "count": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    )

    return 12
