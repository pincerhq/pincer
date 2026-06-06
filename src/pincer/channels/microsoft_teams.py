"""
Microsoft Teams channel implementation using the microsoft-teams-apps SDK.

Architecture:
  Teams ──HTTP POST /api/messages──> uvicorn + FastAPIAdapter ──> App.on_message ──> Pincer Agent

Teams delivers activities by POSTing them to a public endpoint, so the channel runs a
small uvicorn server instead of holding an outbound connection like Slack or Discord.

Features:
- Personal (DM) conversations
- @mention in channels (replies in a thread)
- Existing thread replies (each thread is its own session)
- Group chats
- Optional user allowlist
- Proactive delivery via stored conversation references
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

MAX_TEAMS_MESSAGE_LENGTH = 8000  # Teams allows ~28k; keep replies readable

_MENTION_RE = re.compile(r"<at>[^<]*</at>")


def split_message(text: str, max_len: int = MAX_TEAMS_MESSAGE_LENGTH) -> list[str]:
    """Split long text at paragraph boundaries, respecting Teams' message limit."""
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
            if len(paragraph) <= max_len:
                current = paragraph
            else:
                for line in paragraph.split("\n"):
                    if not current:
                        if len(line) <= max_len:
                            current = line
                        else:
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


class MicrosoftTeamsChannel(BaseChannel):
    """Microsoft Teams as a Pincer channel via the microsoft-teams-apps SDK.

    Credentials come from the Azure Bot / App Registration:
    - PINCER_TEAMS_APP_ID        Microsoft App (client) ID
    - PINCER_TEAMS_APP_PASSWORD  App Password (client secret)
    - PINCER_TEAMS_PORT          local server port (default 3978)
    """

    channel_type = ChannelType.TEAMS

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._app: Any = None
        self._fastapi: Any = None
        self._server: Any = None
        self._server_task: asyncio.Task[Any] | None = None
        self._conversation_refs: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "teams"

    async def start(self, handler: MessageHandler) -> None:
        app_id = self._settings.teams_app_id
        app_password = self._settings.teams_app_password.get_secret_value()

        if not app_id or not app_password:
            logger.warning(
                "Teams channel disabled: PINCER_TEAMS_APP_ID and PINCER_TEAMS_APP_PASSWORD required."
            )
            return

        self._handler = handler

        try:
            import uvicorn
            from fastapi import FastAPI
            from microsoft_teams.apps import App, FastAPIAdapter
        except ImportError:
            logger.error("microsoft-teams-apps not installed. Run: pip install 'pincer-agent[teams]'")
            return

        self._fastapi = FastAPI(title="Pincer Teams Bot")
        adapter = FastAPIAdapter(app=self._fastapi)
        self._app = App(
            client_id=app_id,
            client_secret=app_password,
            http_server_adapter=adapter,
        )

        @self._app.on_message  # type: ignore[misc, untyped-decorator]
        async def _on_message(ctx: Any) -> None:
            await self._handle_activity(ctx)

        # Mount the /api/messages route on the FastAPI app; we own the server.
        await self._app.initialize()

        config = uvicorn.Config(
            app=self._fastapi,
            host="0.0.0.0",
            port=self._settings.teams_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        logger.info("Teams channel started (HTTP server on port %s)", self._settings.teams_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()
            except Exception as e:
                logger.debug("Teams server shutdown error: %s", e)
        self._server_task = None
        logger.info("Teams channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a proactive message to a user.

        Teams requires a prior interaction before the bot can message a user, so this
        looks up the conversation reference stored from an earlier incoming activity.
        Pass conversation_id in kwargs to target a specific conversation directly.
        """
        if not self._app:
            logger.warning("Teams app not initialized, cannot send")
            return

        conversation_id: str | None = kwargs.get("conversation_id") or self._conversation_refs.get(
            user_id
        )
        if not conversation_id:
            logger.warning("Teams: no conversation reference for %s; cannot send", user_id)
            return

        try:
            from microsoft_teams.api import MessageActivityInput
        except ImportError:
            logger.error("microsoft-teams-apps not installed; cannot send Teams message")
            return

        for chunk in split_message(text):
            try:
                await self._app.send(conversation_id, MessageActivityInput(text=chunk))
            except Exception as e:
                logger.error("Teams proactive send failed: %s", e)

    async def _handle_activity(self, ctx: Any) -> None:
        activity = getattr(ctx, "activity", None)
        if activity is None:
            return

        user_id = self._activity_user_id(activity)
        text = getattr(activity, "text", "") or ""

        if self._settings.teams_user_allowlist and user_id not in self._settings.teams_user_allowlist:
            logger.debug("Teams user %s not in allowlist, ignoring", user_id)
            return

        text = _MENTION_RE.sub("", text).strip()
        if not text:
            return

        session_key = self._make_session_key(activity)
        self._store_conversation_ref(user_id, activity)

        incoming = IncomingMessage(
            user_id=user_id,
            channel=session_key,
            text=text,
            channel_type=ChannelType.TEAMS,
            raw=activity,
        )

        if not self._handler:
            return

        try:
            response = await self._handler(incoming)
        except Exception:
            logger.exception("Agent error for Teams user %s", user_id)
            response = "⚠️ Something went wrong. Please try again."

        if response:
            for chunk in split_message(response):
                try:
                    await ctx.send(chunk)
                except Exception as e:
                    logger.error("Teams reply failed: %s", e)
                    break

    @staticmethod
    def _activity_user_id(activity: Any) -> str:
        sender = getattr(activity, "from_", None)
        if sender is None:
            return ""
        return getattr(sender, "aad_object_id", "") or getattr(sender, "id", "") or ""

    def _make_session_key(self, activity: Any) -> str:
        """Build the session key for an activity.

        DM -> teams-dm-{aad}, group chat -> teams-chat-{chat_id},
        new channel message -> teams-thread-{activity_id},
        channel thread reply -> teams-thread-{thread_root}.
        """
        conversation = getattr(activity, "conversation", None)
        conv_type = (getattr(conversation, "conversation_type", "") or "").lower()
        conv_id = getattr(conversation, "id", "") or ""
        activity_id = getattr(activity, "id", "") or ""

        if conv_type == "personal":
            return f"teams-dm-{self._activity_user_id(activity)}"
        if conv_type == "groupchat":
            return f"teams-chat-{conv_id}"

        # Channel ids encode the thread root as "...;messageid=<root>" once a thread exists.
        if ";messageid=" in conv_id:
            return f"teams-thread-{conv_id.split(';messageid=', 1)[1]}"
        return f"teams-thread-{activity_id}"

    def _store_conversation_ref(self, user_id: str, activity: Any) -> None:
        if not user_id:
            return
        conversation = getattr(activity, "conversation", None)
        conv_id = getattr(conversation, "id", "") or ""
        if conv_id:
            self._conversation_refs[user_id] = conv_id
