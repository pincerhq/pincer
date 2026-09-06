"""
Slack channel implementation using slack-bolt + Socket Mode.

Architecture:
  Slack API ←→ Socket Mode WebSocket ←→ slack-bolt AsyncApp ←→ Pincer Agent

Features:
- DM conversations (full agent interaction)
- @mention in channels (creates thread reply)
- Thread-based conversations (each thread = separate session context)
- File/image uploads → vision pipeline
- Approval flow via Block Kit interactive buttons
- Rich mrkdwn formatting
- Workspace and user allowlists (optional)
- Proactive message delivery via DM
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler
from pincer.channels.slack_formatters import build_approval_blocks, markdown_to_mrkdwn

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

MAX_SLACK_MESSAGE_LENGTH = 3900  # Slack hard limit is 4000; leave headroom


def split_message(text: str, max_len: int = MAX_SLACK_MESSAGE_LENGTH) -> list[str]:
    """Split long text at paragraph boundaries, respecting Slack's limit."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).lstrip("\n") if current else paragraph
        if len(candidate) > max_len:
            if current:
                chunks.append(current.strip())
                current = ""
            # Paragraph itself might be too long — try splitting at line boundaries
            if len(paragraph) <= max_len:
                current = paragraph
            else:
                for line in paragraph.split("\n"):
                    if not current:
                        if len(line) <= max_len:
                            current = line
                        else:
                            # Hard-cut individual line
                            pos = 0
                            while pos < len(line):
                                chunks.append(line[pos : pos + max_len])
                                pos += max_len
                    elif len(current) + 1 + len(line) <= max_len:
                        current = current + "\n" + line
                    else:
                        chunks.append(current.strip())
                        current = line if len(line) <= max_len else ""
                        if len(line) > max_len:
                            pos = 0
                            while pos < len(line):
                                chunks.append(line[pos : pos + max_len])
                                pos += max_len
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_len]]


class SlackChannel(BaseChannel):
    """
    Slack as a Pincer messaging channel via Socket Mode.

    Token requirements:
    - PINCER_SLACK_BOT_TOKEN   xoxb-...  (Bot User OAuth Token)
    - PINCER_SLACK_APP_TOKEN   xapp-...  (App-Level Token, scope: connections:write)

    Lifecycle:
    1. __init__ — stores config; no network connections yet
    2. start(handler) — creates slack-bolt AsyncApp, registers listeners,
                        connects via Socket Mode WebSocket
    3. Incoming events → listeners → _process_message → handler → send reply
    4. stop() — closes Socket Mode connection
    """

    channel_type = ChannelType.SLACK

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._app: Any = None  # slack_bolt.app.async_app.AsyncApp
        self._socket_handler: Any = None  # AsyncSocketModeHandler
        self._bot_user_id: str = ""
        # Map approval_id → asyncio.Future[bool] for button interactions
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}

    @property
    def name(self) -> str:
        return "slack"

    async def resolve_internal_user_id(self, identifier: str) -> str:
        """Resolve a Slack identifier to an internal user ID (U...).

        Handles two cases:
        - Already a Slack user ID (starts with "U") → returned as-is.
        - An email address → resolved via users.lookupByEmail (requires the
          users:read.email scope on the bot token).

        Any other format (display name, etc.) is returned unchanged.
        """
        if identifier.startswith("U"):
            return identifier
        if "@" in identifier and self._app is not None:
            try:
                result = await self._app.client.users_lookupByEmail(email=identifier)
                return str(result["user"]["id"])
            except Exception:
                logger.debug("Slack: could not resolve email %r to user_id", identifier)
        return identifier

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self, handler: MessageHandler) -> None:
        """Connect to Slack via Socket Mode and register event listeners."""
        bot_token = self._settings.slack_bot_token.get_secret_value()
        app_token = self._settings.slack_app_token.get_secret_value()

        if not bot_token or not app_token:
            logger.warning(
                "Slack channel disabled: PINCER_SLACK_BOT_TOKEN and PINCER_SLACK_APP_TOKEN required. "
                "Run 'pincer slack setup' to configure."
            )
            return

        self._handler = handler

        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.app.async_app import AsyncApp
        except ImportError:
            logger.error("slack-bolt not installed. Run: pip install 'pincer-agent[slack]'")
            return

        self._app = AsyncApp(token=bot_token, logger=logging.getLogger("slack_bolt"))

        # Discover bot's own user ID (used to ignore self-messages and strip mentions)
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=bot_token)
        try:
            auth = await client.auth_test()
            self._bot_user_id = auth["user_id"]
            logger.info("Slack bot authenticated as %s (%s)", auth["user"], self._bot_user_id)
        except Exception as e:
            logger.error("Slack auth_test failed: %s", e)
            return

        self._register_listeners()

        self._socket_handler = AsyncSocketModeHandler(self._app, app_token)
        await self._socket_handler.connect_async()
        logger.info("Slack channel started (Socket Mode)")

    async def stop(self) -> None:
        if self._socket_handler:
            try:
                await self._socket_handler.close_async()
            except Exception as e:
                logger.debug("Slack Socket Mode close error: %s", e)
        logger.info("Slack channel stopped")

    # ── Sending ──────────────────────────────────────────────────────────────

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a message to a Slack channel/DM or open a new DM for proactive delivery.

        user_id: Slack user ID (U...) or channel ID (C..., D...).
        For proactive messages (briefings, etc.) pass the Slack user ID —
        this method will open a DM conversation automatically.
        kwargs:
          thread_ts: str — reply in this thread timestamp
          channel_id: str — explicit channel override

        Raises on failure so callers can tell delivery didn't happen —
        previously every failure here (including an unresolvable user/channel
        id) was logged and swallowed, so tool-invoked sends like
        send_file/send_image reported false success back to the LLM (issue
        #162). The one internal auto-reply call site (`_process_message`)
        wraps this itself to keep its existing log-and-continue resilience.
        """
        if not self._app:
            raise RuntimeError("Slack app not initialized, cannot send")

        thread_ts: str | None = kwargs.get("thread_ts")
        channel_id: str | None = kwargs.get("channel_id") or (
            user_id if (user_id.startswith("C") or user_id.startswith("D")) else None
        )

        # If we only have a user ID (U...), open or retrieve the DM channel
        if not channel_id:
            try:
                result = await self._app.client.conversations_open(users=[user_id])
                channel_id = result["channel"]["id"]
            except Exception as e:
                raise RuntimeError(f"Slack conversations_open failed for {user_id!r}: {e}") from e

        formatted = markdown_to_mrkdwn(text)
        for chunk in split_message(formatted):
            await self._app.client.chat_postMessage(
                channel=channel_id,
                text=chunk,
                thread_ts=thread_ts,
                unfurl_links=False,
            )

    async def send_approval_request(
        self,
        channel_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        approval_id: str,
        thread_ts: str | None = None,
    ) -> None:
        """Send an approval prompt with ✅/❌ Block Kit buttons."""
        if not self._app:
            return
        blocks = build_approval_blocks(tool_name, tool_args, approval_id)
        try:
            await self._app.client.chat_postMessage(
                channel=channel_id,
                text=f"🔐 Approval needed: `{tool_name}`",
                blocks=blocks,
                thread_ts=thread_ts,
            )
        except Exception as e:
            logger.error("Slack send_approval_request failed: %s", e)

    # ── Listeners ────────────────────────────────────────────────────────────

    def _register_listeners(self) -> None:
        """Register all slack-bolt event and action listeners."""

        @self._app.event("message")  # type: ignore[untyped-decorator]
        async def handle_message(event: dict[str, Any], client: Any) -> None:
            # Ignore own messages and all bot messages
            if event.get("user") == self._bot_user_id:
                return
            if event.get("bot_id"):
                return
            # Ignore subtypes (edits, deletes, joins, file shares without text, etc.)
            if event.get("subtype"):
                return

            channel_type = event.get("channel_type", "")
            text = event.get("text", "") or ""

            if channel_type == "im":
                # Direct message — always respond
                await self._process_message(event, client)
            elif f"<@{self._bot_user_id}>" in text:
                # Channel message that mentions the bot (belt-and-suspenders for app_mention)
                await self._process_message(event, client)

        @self._app.event("app_mention")  # type: ignore[untyped-decorator]
        async def handle_mention(event: dict[str, Any], client: Any) -> None:
            """Handle @pincer mentions in channels."""
            if event.get("user") == self._bot_user_id:
                return
            await self._process_message(event, client)

        @self._app.action("pincer_approve")  # type: ignore[untyped-decorator]
        async def handle_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
            await ack()
            approval_id = body["actions"][0].get("value", "")
            await self._resolve_approval(approval_id, approved=True, body=body, client=client)

        @self._app.action("pincer_deny")  # type: ignore[untyped-decorator]
        async def handle_deny(ack: Any, body: dict[str, Any], client: Any) -> None:
            await ack()
            approval_id = body["actions"][0].get("value", "")
            await self._resolve_approval(approval_id, approved=False, body=body, client=client)

    # ── Message processing ───────────────────────────────────────────────────

    async def _process_message(self, event: dict[str, Any], client: Any) -> None:
        """Process one incoming Slack message through the agent pipeline."""
        slack_user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "") or ""

        # Strip bot mention from text
        text = re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

        # Handle file-only messages (no text but files attached)
        files = event.get("files", [])
        if not text and not files:
            return

        # Determine thread context
        # thread_ts is set when this message is already inside a thread
        # ts is the message's own timestamp
        existing_thread_ts: str | None = event.get("thread_ts")
        msg_ts: str = event.get("ts", "")

        # Session key: threads get their own context; DMs share one context
        channel_type = event.get("channel_type", "")
        if channel_type == "im":
            session_key = f"slack-dm-{slack_user_id}"
            reply_thread_ts: str | None = None  # DMs don't thread
        elif existing_thread_ts:
            # Message is inside an existing thread — continue that thread's session
            session_key = f"slack-thread-{existing_thread_ts}"
            reply_thread_ts = existing_thread_ts
        else:
            # New message in a channel — start a thread from this message
            session_key = f"slack-thread-{msg_ts}"
            reply_thread_ts = msg_ts

        # Process image attachments
        images: list[tuple[bytes, str]] = []
        for f in files:
            mime = f.get("mimetype", "") or ""
            if mime.startswith("image/"):
                data = await self._download_file(f.get("url_private", ""), client)
                if data:
                    images.append((data, mime))

        incoming = IncomingMessage(
            user_id=slack_user_id,
            channel=session_key,
            text=text,
            images=images,
            channel_type=ChannelType.SLACK,
            raw=event,
        )

        if not self._handler:
            return

        try:
            response = await self._handler(incoming)
        except Exception:
            logger.exception("Agent error for Slack user %s", slack_user_id)
            response = "⚠️ Something went wrong. Please try again."

        if response:
            try:
                await self.send(
                    user_id=channel_id,
                    text=response,
                    channel_id=channel_id,
                    thread_ts=reply_thread_ts,
                )
            except Exception as exc:
                logger.error("Slack reply send failed: %s", exc)

    # ── Approval flow ────────────────────────────────────────────────────────

    async def _resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        body: dict[str, Any],
        client: Any,
    ) -> None:
        """Update the approval message and resolve the pending future."""
        channel = body.get("channel", {}).get("id", "")
        ts = body.get("message", {}).get("ts", "")
        user_name = body.get("user", {}).get("name", "someone")
        status = "✅ Approved" if approved else "❌ Denied"

        if channel and ts:
            try:
                await client.chat_update(
                    channel=channel,
                    ts=ts,
                    text=f"{status} by {user_name}",
                    blocks=[],
                )
            except Exception as e:
                logger.debug("Slack chat_update failed: %s", e)

        fut = self._pending_approvals.get(approval_id)
        if fut and not fut.done():
            fut.set_result(approved)

    async def request_approval(
        self,
        channel_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        approval_id: str,
        thread_ts: str | None = None,
        timeout: float = 120.0,
    ) -> bool:
        """Send approval buttons and wait for user's response (True = approved)."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_approvals[approval_id] = fut

        await self.send_approval_request(channel_id, tool_name, tool_args, approval_id, thread_ts)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            logger.warning("Slack approval timed out for %s", approval_id)
            return False
        finally:
            self._pending_approvals.pop(approval_id, None)

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _get_display_name(self, user_id: str, client: Any) -> str:
        try:
            result = await client.users_info(user=user_id)
            profile = result["user"]["profile"]
            return profile.get("display_name") or profile.get("real_name") or user_id
        except Exception:
            return user_id

    async def _download_file(self, url: str, client: Any) -> bytes | None:
        """Download a private Slack file using the bot token for auth."""
        if not url:
            return None
        bot_token = self._settings.slack_bot_token.get_secret_value()
        try:
            import aiohttp

            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"Authorization": f"Bearer {bot_token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp,
            ):
                if resp.status == 200:
                    return await resp.read()
                logger.debug("Slack file download HTTP %s for %s", resp.status, url)
        except ImportError:
            logger.debug("aiohttp not available; Slack file downloads disabled")
        except Exception as e:
            logger.error("Slack file download error: %s", e)
        return None
