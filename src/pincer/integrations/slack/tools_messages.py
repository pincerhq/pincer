"""Message / thread tools — 18 tools.

All functions are async, accept a SlackClient as first arg, and return str.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_message(m: dict[str, Any]) -> str:
    ts = m.get("ts", "?")
    user = m.get("user") or m.get("bot_id", "?")
    text = m.get("text", "")[:300]
    reply_count = m.get("reply_count", 0)
    suffix = f" [{reply_count} replies]" if reply_count else ""
    return f"[{ts}] {user}: {text}{suffix}"


# ── tool functions ─────────────────────────────────────────────────────────────


async def slack__get_channel_history(
    client: SlackClient,
    channel: str,
    limit: int = 20,
    oldest: str = "",
    latest: str = "",
) -> str:
    """Get recent messages from a channel."""
    try:
        kwargs: dict[str, Any] = {"channel": channel, "limit": min(limit, 200)}
        if oldest:
            kwargs["oldest"] = oldest
        if latest:
            kwargs["latest"] = latest
        resp = await client.bot.conversations_history(**kwargs)
        messages = resp.get("messages", [])
        if not messages:
            return f"No messages found in {channel}."
        lines = [_fmt_message(m) for m in messages]
        has_more = resp.get("has_more", False)
        result = f"Last {len(lines)} message(s) in {channel}:\n" + "\n".join(lines)
        if has_more:
            result += "\n(More messages available — use oldest/latest params to paginate)"
        return result
    except Exception as exc:
        return f"Error getting channel history: {exc}"


async def slack__get_thread_replies(
    client: SlackClient, channel: str, thread_ts: str
) -> str:
    """Get all replies in a thread."""
    try:
        resp = await client.bot.conversations_replies(channel=channel, ts=thread_ts)
        messages = resp.get("messages", [])
        if not messages:
            return f"No replies found for thread {thread_ts}."
        lines = [_fmt_message(m) for m in messages]
        return f"Thread {thread_ts} — {len(lines)} message(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error getting thread replies: {exc}"


async def slack__get_message_permalink(
    client: SlackClient, channel: str, message_ts: str
) -> str:
    """Get a permanent link to a specific message."""
    try:
        resp = await client.bot.chat_getPermalink(channel=channel, message_ts=message_ts)
        return f"Permalink: {resp.get('permalink', '?')}"
    except Exception as exc:
        return f"Error getting permalink: {exc}"


async def slack__post_message(
    client: SlackClient,
    channel: str,
    text: str,
    thread_ts: str = "",
    username: str = "",
    icon_emoji: str = "",
) -> str:
    """Send a message to a channel or DM."""
    try:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if username:
            kwargs["username"] = username
        if icon_emoji:
            kwargs["icon_emoji"] = icon_emoji
        resp = await client.bot.chat_postMessage(**kwargs)
        ts = resp.get("ts", "?")
        return f"Message posted to {channel} (ts: {ts})"
    except Exception as exc:
        return f"Error posting message: {exc}"


async def slack__post_threaded_reply(
    client: SlackClient,
    channel: str,
    thread_ts: str,
    text: str,
) -> str:
    """Reply in an existing thread."""
    try:
        resp = await client.bot.chat_postMessage(
            channel=channel, text=text, thread_ts=thread_ts
        )
        ts = resp.get("ts", "?")
        return f"Reply posted in thread {thread_ts} (ts: {ts})"
    except Exception as exc:
        return f"Error posting threaded reply: {exc}"


async def slack__post_ephemeral(
    client: SlackClient, channel: str, user: str, text: str
) -> str:
    """Send a message visible only to one user in a channel."""
    try:
        await client.bot.chat_postEphemeral(channel=channel, user=user, text=text)
        return f"Ephemeral message sent to {user} in {channel}."
    except Exception as exc:
        return f"Error posting ephemeral message: {exc}"


async def slack__update_message(
    client: SlackClient, channel: str, ts: str, text: str
) -> str:
    """Edit an existing message."""
    try:
        resp = await client.bot.chat_update(channel=channel, ts=ts, text=text)
        return f"Message {resp.get('ts', ts)} updated in {channel}."
    except Exception as exc:
        return f"Error updating message: {exc}"


async def slack__delete_message(client: SlackClient, channel: str, ts: str) -> str:
    """Delete a message from a channel."""
    try:
        await client.bot.chat_delete(channel=channel, ts=ts)
        return f"Message {ts} deleted from {channel}."
    except Exception as exc:
        return f"Error deleting message: {exc}"


async def slack__schedule_message(
    client: SlackClient,
    channel: str,
    text: str,
    post_at: int,
) -> str:
    """Schedule a message for future delivery.

    post_at is a Unix timestamp (seconds since epoch).
    """
    try:
        resp = await client.bot.chat_scheduleMessage(
            channel=channel, text=text, post_at=post_at
        )
        return (
            f"Message scheduled for {channel} at unix timestamp {post_at} "
            f"(scheduled_message_id: {resp.get('scheduled_message_id', '?')})"
        )
    except Exception as exc:
        return f"Error scheduling message: {exc}"


async def slack__list_scheduled_messages(
    client: SlackClient, channel: str = ""
) -> str:
    """List pending scheduled messages."""
    try:
        kwargs: dict[str, Any] = {}
        if channel:
            kwargs["channel"] = channel
        resp = await client.bot.chat_scheduledMessages_list(**kwargs)
        msgs = resp.get("scheduled_messages", [])
        if not msgs:
            return "No scheduled messages found."
        lines = []
        for m in msgs:
            lines.append(
                f"ID: {m.get('id')} | channel: {m.get('channel_id')} | "
                f"post_at: {m.get('post_at')} | text: {str(m.get('text', ''))[:80]}"
            )
        return f"{len(lines)} scheduled message(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing scheduled messages: {exc}"


async def slack__delete_scheduled_message(
    client: SlackClient, channel: str, scheduled_message_id: str
) -> str:
    """Cancel a scheduled message before it is sent."""
    try:
        await client.bot.chat_deleteScheduledMessage(
            channel=channel, scheduled_message_id=scheduled_message_id
        )
        return f"Scheduled message {scheduled_message_id} cancelled."
    except Exception as exc:
        return f"Error deleting scheduled message: {exc}"


async def slack__post_message_with_blocks(
    client: SlackClient,
    channel: str,
    blocks_json: str,
    text: str = "",
) -> str:
    """Send a rich Block Kit message (JSON array of block objects).

    blocks_json should be a JSON-serialised list of Slack Block Kit blocks.
    """
    try:
        import json

        blocks = json.loads(blocks_json)
        resp = await client.bot.chat_postMessage(
            channel=channel, blocks=blocks, text=text
        )
        return f"Block Kit message posted to {channel} (ts: {resp.get('ts', '?')})"
    except Exception as exc:
        return f"Error posting block message: {exc}"


async def slack__share_message(
    client: SlackClient,
    channel: str,
    source_channel: str,
    message_ts: str,
    comment: str = "",
) -> str:
    """Forward/share a message to another channel by its timestamp."""
    try:
        # Get the permalink for the original message and attach it
        perm_resp = await client.bot.chat_getPermalink(
            channel=source_channel, message_ts=message_ts
        )
        permalink = perm_resp.get("permalink", "")
        text = comment or "Shared message:"
        attachments = [{"text": "", "footer": permalink}]
        resp = await client.bot.chat_postMessage(
            channel=channel, text=text, attachments=attachments
        )
        return f"Message shared to {channel} (ts: {resp.get('ts', '?')})"
    except Exception as exc:
        return f"Error sharing message: {exc}"


async def slack__post_dm(
    client: SlackClient,
    user: str,
    text: str,
) -> str:
    """Send a direct message to a user by user ID or email.

    If user looks like an email address, it is resolved to a user ID first.
    """
    try:
        # Resolve email → user ID if needed
        uid = user
        if "@" in user:
            resp = await client.bot.users_lookupByEmail(email=user)
            uid = resp.get("user", {}).get("id", user)
        # Open DM channel
        dm_resp = await client.bot.conversations_open(users=[uid])
        channel_id = dm_resp.get("channel", {}).get("id", "")
        resp = await client.bot.chat_postMessage(channel=channel_id, text=text)
        return f"DM sent to {user} (channel: {channel_id}, ts: {resp.get('ts', '?')})"
    except Exception as exc:
        return f"Error sending DM: {exc}"


async def slack__post_broadcast_reply(
    client: SlackClient,
    channel: str,
    thread_ts: str,
    text: str,
) -> str:
    """Reply in a thread AND post the reply to the channel as well."""
    try:
        resp = await client.bot.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            reply_broadcast=True,
        )
        return f"Broadcast reply posted in thread {thread_ts} and channel (ts: {resp.get('ts', '?')})"
    except Exception as exc:
        return f"Error posting broadcast reply: {exc}"


async def slack__get_unread_messages(
    client: SlackClient, max_channels: int = 10
) -> str:
    """Get channels with unread messages and their counts."""
    try:
        from pincer.integrations.slack.pagination import paginate

        channels = await paginate(
            client.bot.conversations_list,
            "channels",
            limit=200,
            types="public_channel,private_channel",
            exclude_archived=True,
        )
        unread = []
        for c in channels:
            count = c.get("unread_count", 0)
            if count and count > 0:
                unread.append(
                    f"#{c.get('name', '?')} (ID: {c.get('id')}) — {count} unread"
                )
        if not unread:
            return "No unread messages found."
        return f"Channels with unread messages ({len(unread)}):\n" + "\n".join(unread[:max_channels])
    except Exception as exc:
        return f"Error getting unread messages: {exc}"


async def slack__mark_channel_read(client: SlackClient, channel: str, ts: str) -> str:
    """Mark a channel as read up to the given message timestamp."""
    try:
        await client.bot.conversations_mark(channel=channel, ts=ts)
        return f"Channel {channel} marked as read up to {ts}."
    except Exception as exc:
        return f"Error marking channel read: {exc}"


async def slack__summarize_channel(
    client: SlackClient, channel: str, limit: int = 20
) -> str:
    """Fetch the last N messages from a channel for summarisation.

    Returns formatted message history — the calling agent should summarise it.
    """
    try:
        resp = await client.bot.conversations_history(
            channel=channel, limit=min(limit, 100)
        )
        messages = resp.get("messages", [])
        if not messages:
            return f"No messages found in {channel}."
        lines = [_fmt_message(m) for m in reversed(messages)]
        header = f"Last {len(lines)} message(s) in {channel} (oldest first):"
        return header + "\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error fetching channel for summary: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_message_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 18 message tools. Returns 18."""

    def _h(
        fn: Callable[..., Awaitable[str]],
    ) -> Callable[..., Awaitable[str]]:
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__get_channel_history",
        description="Get recent messages from a Slack channel, with optional time range.",
        handler=_h(slack__get_channel_history),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "limit": {"type": "integer", "description": "Number of messages (max 200)", "default": 20},
                "oldest": {"type": "string", "description": "Oldest message timestamp (Unix epoch)", "default": ""},
                "latest": {"type": "string", "description": "Latest message timestamp (Unix epoch)", "default": ""},
            },
            "required": ["channel"],
        },
    )
    registry.register(
        name="slack__get_thread_replies",
        description="Get all replies in a thread.",
        handler=_h(slack__get_thread_replies),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID containing the thread"},
                "thread_ts": {"type": "string", "description": "Timestamp of the parent message"},
            },
            "required": ["channel", "thread_ts"],
        },
    )
    registry.register(
        name="slack__get_message_permalink",
        description="Get a permanent URL link to a specific message.",
        handler=_h(slack__get_message_permalink),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "message_ts": {"type": "string", "description": "Message timestamp"},
            },
            "required": ["channel", "message_ts"],
        },
    )
    registry.register(
        name="slack__post_message",
        description="Send a text message to a Slack channel or DM.",
        handler=_h(slack__post_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID or name"},
                "text": {"type": "string", "description": "Message text"},
                "thread_ts": {"type": "string", "description": "Reply in thread (parent ts)", "default": ""},
                "username": {"type": "string", "description": "Custom display name", "default": ""},
                "icon_emoji": {"type": "string", "description": "Custom icon emoji", "default": ""},
            },
            "required": ["channel", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__post_threaded_reply",
        description="Post a reply in an existing Slack thread.",
        handler=_h(slack__post_threaded_reply),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "thread_ts": {"type": "string", "description": "Parent message timestamp"},
                "text": {"type": "string", "description": "Reply text"},
            },
            "required": ["channel", "thread_ts", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__post_ephemeral",
        description="Send a message visible only to one user in a channel.",
        handler=_h(slack__post_ephemeral),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "user": {"type": "string", "description": "User ID to show message to"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["channel", "user", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__update_message",
        description="Edit the text of an existing message.",
        handler=_h(slack__update_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "ts": {"type": "string", "description": "Message timestamp"},
                "text": {"type": "string", "description": "New message text"},
            },
            "required": ["channel", "ts", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__delete_message",
        description="Delete a message from a Slack channel.",
        handler=_h(slack__delete_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "ts": {"type": "string", "description": "Message timestamp"},
            },
            "required": ["channel", "ts"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__schedule_message",
        description="Schedule a message to be sent at a future time.",
        handler=_h(slack__schedule_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "text": {"type": "string", "description": "Message text"},
                "post_at": {"type": "integer", "description": "Unix timestamp when to send"},
            },
            "required": ["channel", "text", "post_at"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__list_scheduled_messages",
        description="List all pending scheduled messages, optionally filtered by channel.",
        handler=_h(slack__list_scheduled_messages),
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Filter by channel ID", "default": ""}},
            "required": [],
        },
    )
    registry.register(
        name="slack__delete_scheduled_message",
        description="Cancel a scheduled message before it is sent.",
        handler=_h(slack__delete_scheduled_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "scheduled_message_id": {"type": "string", "description": "Scheduled message ID"},
            },
            "required": ["channel", "scheduled_message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__post_message_with_blocks",
        description=(
            "Send a rich Block Kit message (JSON array of block objects). "
            "blocks_json must be a valid JSON string representing a list of Slack blocks."
        ),
        handler=_h(slack__post_message_with_blocks),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "blocks_json": {"type": "string", "description": "JSON array of Block Kit block objects"},
                "text": {"type": "string", "description": "Fallback text for notifications", "default": ""},
            },
            "required": ["channel", "blocks_json"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__share_message",
        description="Forward/share a message to another channel.",
        handler=_h(slack__share_message),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Destination channel ID"},
                "source_channel": {"type": "string", "description": "Source channel ID"},
                "message_ts": {"type": "string", "description": "Timestamp of message to share"},
                "comment": {"type": "string", "description": "Optional comment to include", "default": ""},
            },
            "required": ["channel", "source_channel", "message_ts"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__post_dm",
        description="Send a direct message to a user by their user ID or email address.",
        handler=_h(slack__post_dm),
        parameters={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "User ID (U...) or email address"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["user", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__post_broadcast_reply",
        description="Reply in a thread AND also post the reply to the main channel.",
        handler=_h(slack__post_broadcast_reply),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "thread_ts": {"type": "string", "description": "Parent message timestamp"},
                "text": {"type": "string", "description": "Reply text"},
            },
            "required": ["channel", "thread_ts", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__get_unread_messages",
        description="List channels that have unread messages along with unread counts.",
        handler=_h(slack__get_unread_messages),
        parameters={
            "type": "object",
            "properties": {"max_channels": {"type": "integer", "default": 10}},
            "required": [],
        },
    )
    registry.register(
        name="slack__mark_channel_read",
        description="Mark a channel as read up to the given message timestamp.",
        handler=_h(slack__mark_channel_read),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "ts": {"type": "string", "description": "Timestamp to mark read up to"},
            },
            "required": ["channel", "ts"],
        },
    )
    registry.register(
        name="slack__summarize_channel",
        description=(
            "Fetch the last N messages from a channel. "
            "Returns formatted history for summarisation by the agent."
        ),
        handler=_h(slack__summarize_channel),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "limit": {"type": "integer", "description": "Number of messages to fetch (max 100)", "default": 20},
            },
            "required": ["channel"],
        },
    )

    return 18
