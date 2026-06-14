"""
Microsoft Teams channel implementation using the microsoft-teams-apps SDK.

Architecture:
  Teams ──HTTP POST /messages──> uvicorn + FastAPIAdapter ──> App.on_message ──> Pincer Agent

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
import json as _json
import logging
import re
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

MAX_TEAMS_MESSAGE_LENGTH = 8000  # Teams allows ~28k; keep replies readable

_MENTION_RE = re.compile(r"<at>[^<]*</at>")

# Activity types the microsoft-teams-apps SDK can parse without Pydantic errors.
# Anything outside this set is ACKed with 200 OK so Teams stops retrying.
_SDK_KNOWN_TYPES: frozenset[str] = frozenset({
    "message",
    "typing",
    "conversationUpdate",
    "messageReaction",
    "messageUpdate",
    "messageDelete",
    "invoke",
    "installationUpdate",
    "event",
    "command",
    "commandResult",
})


class _ActivityFilterMiddleware:
    """ASGI middleware: intercepts POST /messages before the SDK.

    When Teams sends an activity type the SDK cannot parse it logs 14 Pydantic
    validation errors and (depending on SDK version) returns 400, which causes
    Teams to retry indefinitely.  This middleware ACKs unrecognised types with
    200 OK immediately; known types are forwarded to the SDK unchanged.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("path") != "/messages"
            or scope.get("method") != "POST"
        ):
            await self._app(scope, receive, send)
            return

        # Buffer the request body — consumed once here, must be replayed.
        chunks: list[bytes] = []
        more = True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)

        activity_type = ""
        try:
            activity_type = _json.loads(body).get("type", "")
        except Exception:
            pass

        if activity_type and activity_type not in _SDK_KNOWN_TYPES:
            logger.debug("Teams: silently ACKing unhandled activity type %r", activity_type)
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b'{"status":"ok"}', "more_body": False})
            return

        # Replay the buffered body so the SDK route handler can read it.
        replayed = False

        async def _replay_receive() -> dict:  # type: ignore[return]
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self._app(scope, _replay_receive, send)


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
    - PINCER_TEAMS_APP_TENANTID  Microsoft App tenant ID, only for single tenant config
    - PINCER_TEAMS_APP_PASSWORD  App Password (client secret)
    """

    channel_type = ChannelType.TEAMS

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._app: Any = None
        self._fastapi: Any = None
        self._conversation_refs: dict[str, str] = {}
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._pending_inputs: dict[str, asyncio.Future[str]] = {}
        # Active ctx.send callbacks, keyed by user_id, set while a message is
        # being handled so approval/input prompts can reply in-thread instead
        # of going through the proactive send API (which 400s on thread convs).
        self._active_reply_fns: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "teams"

    async def resolve_internal_user_id(self, identifier: str) -> str:
        _identifier = identifier.replace("@", "_")
        try:
            graph = self._app.get_app_graph()
            result = await graph.users.get()
            for u in result.value:
                if _identifier in u.user_principal_name or identifier in u.mail:
                    return u.id

        except Exception:
            logger.error("MS Teams: could not resolve identifier %r to user_id", identifier)
        return identifier

    def get_sub_app(self) -> Any:
        """Return the initialized FastAPI sub-app, or None if not yet initialized."""
        return self._fastapi

    async def _init_app(self, handler: MessageHandler) -> None:
        """Create the FastAPI sub-app and wire up the Teams SDK. No server started.

        Raises RuntimeError if the SDK is not installed or credentials are missing.
        Idempotent — safe to call multiple times.
        """
        if self._fastapi is not None:
            return

        try:
            from fastapi import FastAPI
            from microsoft_teams.apps import App, FastAPIAdapter
        except ImportError as e:
            raise RuntimeError(
                "microsoft-teams-apps not installed. Run: pip install 'pincer-agent[teams]'"
            ) from e

        app_id = self._settings.teams_app_id
        app_tenant_id = self._settings.teams_app_tenant_id
        app_password = self._settings.teams_app_password.get_secret_value()

        if not app_id or not app_password:
            raise RuntimeError(
                "Teams channel cannot start: PINCER_TEAMS_APP_ID and PINCER_TEAMS_APP_PASSWORD are required."
            )

        if app_tenant_id:
            logger.info("Teams channel running in single-tenant mode")
        else:
            logger.info("Teams channel running in multi-tenant mode")

        self._handler = handler

        self._fastapi = FastAPI(title="Pincer Teams Bot")
        self._fastapi.add_middleware(_ActivityFilterMiddleware)
        adapter = FastAPIAdapter(app=self._fastapi)
        self._app = App(
            client_id=app_id,
            client_secret=app_password,
            tenant_id=app_tenant_id,
            http_server_adapter=adapter,
            messaging_endpoint="/messages",
        )

        @self._app.on_message  # type: ignore[misc, untyped-decorator]
        async def _on_message(ctx: Any) -> None:
            await self._handle_activity(ctx)

        await self._app.initialize()

    async def start(self, handler: MessageHandler) -> None:
        """Initialize the Teams SDK sub-app for mounting under the main API server."""
        await self._init_app(handler)
        logger.info("Teams channel initialized (mounted via main API server)")

    async def stop(self) -> None:
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

        # Intercept approval/input responses before routing to the agent.
        approval_fut = self._pending_approvals.get(user_id)
        if approval_fut and not approval_fut.done():
            decision = self._parse_yes_no(text)
            if decision is None:
                try:
                    await ctx.send("Please reply **yes** or **no**.")
                except Exception as e:
                    logger.error("Teams approval re-prompt failed: %s", e)
                return
            approval_fut.set_result(decision)
            return

        input_fut = self._pending_inputs.get(user_id)
        if input_fut and not input_fut.done():
            input_fut.set_result(text)
            return

        incoming = IncomingMessage(
            user_id=user_id,
            channel="teams",         # stable name → identity + memory tag "user:teams:{id}"
            session_id=session_key,  # conversation key → session isolation
            text=text,
            channel_type=ChannelType.TEAMS,
            raw=activity,
        )

        if not self._handler:
            return

        # Expose ctx.send so request_approval / request_input can reply
        # in-thread without hitting the proactive send API.
        self._active_reply_fns[user_id] = ctx.send
        try:
            response = await self._handler(incoming)
        except Exception:
            logger.exception("Agent error for Teams user %s", user_id)
            response = "⚠️ Something went wrong. Please try again."
        finally:
            self._active_reply_fns.pop(user_id, None)

        if response:
            for chunk in split_message(response):
                try:
                    await ctx.send(chunk)
                except Exception as e:
                    logger.error("Teams reply failed: %s", e)
                    break

    # ── Interactive prompts (approval / ask_user) ─────────────────────────────

    async def _send_to_user(self, user_id: str, text: str) -> None:
        """Send a message using the active ctx.send if available, else proactive.

        ctx.send (set while a message is being handled) works for all
        conversation types including channel threads.  The proactive fallback
        only works for personal DMs and may 400 on thread conversations.
        """
        reply_fn = self._active_reply_fns.get(user_id)
        if reply_fn is not None:
            for chunk in split_message(text):
                await reply_fn(chunk)
        else:
            await self.send(user_id, text)

    @staticmethod
    def _parse_yes_no(text: str) -> bool | None:
        t = (text or "").strip().lower()
        if t in {"yes", "y", "ok", "okay", "sure", "approve", "approved", "allow", "👍", "✅"}:
            return True
        if t in {"no", "n", "deny", "denied", "cancel", "stop", "abort", "👎", "❌"}:
            return False
        return None

    async def request_approval(self, user_id: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Send an approval prompt and wait up to 120 s for a yes/no reply."""
        args_preview = ", ".join(f"{k}={v}" for k, v in arguments.items())
        if len(args_preview) > 200:
            args_preview = args_preview[:200] + "…"
        prompt = (
            f"🔐 **Approval required**\n\n"
            f"Tool: `{tool_name}`\n"
            f"Args: `{args_preview}`\n\n"
            f"Reply **yes** or **no**."
        )
        try:
            await self._send_to_user(user_id, prompt)
        except Exception as e:
            logger.warning(
                "Teams approval prompt undeliverable to %s for %s (%s); denying.",
                user_id, tool_name, e,
            )
            return False
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_approvals[user_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=120)
        except TimeoutError:
            logger.info("Teams approval timed out for %s / %s", user_id, tool_name)
            return False
        finally:
            self._pending_approvals.pop(user_id, None)

    async def request_input(self, user_id: str, question: str) -> str:
        """Send a question and wait up to 120 s for the user's reply."""
        try:
            await self._send_to_user(user_id, question)
        except Exception as e:
            logger.warning("Teams input prompt undeliverable to %s (%s)", user_id, e)
            return "[Could not deliver question to user]"
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_inputs[user_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=120)
        except TimeoutError:
            return "[No response from user — timed out after 120s]"
        finally:
            self._pending_inputs.pop(user_id, None)

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
