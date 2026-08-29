"""
WhatsApp channel using neonize (whatsmeow Go backend).

Features:
- QR code pairing on first run (displayed in terminal)
- Text messages, voice notes, images, documents
- Self-chat mode (user messages themselves, agent responds)
- DM allowlist (only approved phone numbers)
- Group chat (respond only when @mentioned or trigger word)
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

try:
    import neonize
    from neonize.aioze import events as _neonize_events
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import (
        ClientOutdatedEv,
        ConnectedEv,
        ConnectFailureEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
        StreamErrorEv,
    )
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as _WAMessageProto
    from neonize.utils import Jid2String, build_jid
    from neonize.utils import log as neonize_log
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia

    HAS_NEONIZE = True
    _NEONIZE_VERSION = getattr(neonize, "__version__", "unknown")
except ImportError:
    HAS_NEONIZE = False
    NewAClient = None  # type: ignore[assignment,misc]
    neonize_log = None  # type: ignore[assignment]
    _neonize_events = None  # type: ignore[assignment]
    _WAMessageProto = None  # type: ignore[assignment,misc]
    ChatPresence = None  # type: ignore[assignment,misc]
    ChatPresenceMedia = None  # type: ignore[assignment,misc]
    _NEONIZE_VERSION = "not installed"

from pincer.channels.base import (
    BaseChannel,
    ChannelType,
    IncomingMessage,
    MessageHandler,
)

if TYPE_CHECKING:
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        Message as WAMessage,
    )

    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)

MAX_WHATSAPP_MESSAGE_LENGTH = 4096

# Remediation text keyed by neonize ConnectFailureReason enum values.
# Kept as plain ints so tests can exercise the map without the neonize stub.
# (2=LOGGED_OUT, 3=TEMP_BANNED, 4=MAIN_DEVICE_GONE, 6=CLIENT_OUTDATED, 7=BAD_USER_AGENT)
_WA_CLIENT_OUTDATED_ACTION = (
    "WhatsApp rejected the client protocol version. "
    "Run `uv pip install -U 'neonize>=0.4.3'` (or `pip install -U neonize`) and restart. "
    "See docs/whatsapp-troubleshooting.md if already on the latest neonize."
)
_WA_LOGGED_OUT_ACTION = (
    "Phone removed the linked device. Delete the neonize session under your data dir "
    "and re-pair with `pincer run --channel whatsapp`."
)
_WA_TEMP_BANNED_ACTION = (
    "WhatsApp temporarily banned this number. Wait out the ban; the Linked Devices "
    "screen in WhatsApp shows the remaining duration."
)
_WA_MAIN_DEVICE_GONE_ACTION = (
    "The primary phone is offline or logged out of WhatsApp. Re-verify the primary "
    "device, then delete the neonize session and re-pair."
)
_WA_BAD_USER_AGENT_ACTION = (
    "WhatsApp rejected the client user-agent — upgrade neonize "
    "(`pip install -U neonize`). See docs/whatsapp-troubleshooting.md if already latest."
)

_WA_CONNECT_FAILURE_REMEDIATION: dict[int, tuple[str, str]] = {
    2: ("logged_out", _WA_LOGGED_OUT_ACTION),
    3: ("temp_banned", _WA_TEMP_BANNED_ACTION),
    4: ("main_device_gone", _WA_MAIN_DEVICE_GONE_ACTION),
    6: ("client_outdated", _WA_CLIENT_OUTDATED_ACTION),
    7: ("bad_user_agent", _WA_BAD_USER_AGENT_ACTION),
}


def _split_whatsapp_message(text: str, max_len: int = MAX_WHATSAPP_MESSAGE_LENGTH) -> list[str]:
    """Split long text at paragraph → line → hard boundaries so nothing is lost."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(paragraph) > max_len:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            for line in paragraph.split("\n"):
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                if len(current) + len(line) + 1 > max_len:
                    if current.strip():
                        chunks.append(current.strip())
                    current = line + "\n"
                else:
                    current += line + "\n"
        elif len(current) + len(paragraph) + 2 > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = paragraph + "\n\n"
        else:
            current += paragraph + "\n\n"

    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:max_len]]


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel using neonize (whatsmeow Go backend)."""

    channel_type = ChannelType.WHATSAPP

    def __init__(self, settings: Settings, identity: IdentityResolver | None = None) -> None:
        if not HAS_NEONIZE:
            raise ImportError(
                "neonize is required for WhatsApp support. Install it with: pip install neonize (requires libmagic)"
            )
        self._settings = settings
        self._identity = identity
        self._client: NewAClient | None = None
        self._handler: MessageHandler | None = None
        self._own_jid: str | None = None
        self._own_jid_full: str | None = None
        self._own_lid: str | None = None
        self._connected = asyncio.Event()
        # Set by login-failure handlers. Consumed by start() to turn silent
        # upstream rejections (err-client-outdated etc.) into loud errors.
        self._login_error: str | None = None
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._pending_inputs: dict[str, asyncio.Future[str]] = {}
        # message_id of approval prompt → user_id; used to accept 👍/👎 reactions.
        self._approval_prompt_ids: dict[str, str] = {}
        # Per-user in-place progress message: user_id → {id, jid, tools}
        self._progress: dict[str, dict[str, Any]] = {}
        # Reply targets learned from inbound messages: sender_phone → (user, server).
        # Needed because sender_phone may be a LID user part that only resolves on
        # the "@lid" server — `build_jid(lid_user)` defaults to "@s.whatsapp.net"
        # and the WA server rejects it with "no LID found".
        self._reply_targets: dict[str, tuple[str, str]] = {}

        # Echo prevention: skip processing messages Pincer just sent.
        self._recent_sent_ids: set[str] = set()
        self._recent_sent_ids_order: deque[str] = deque()
        self._max_recent_sent_ids = 100

    @property
    def name(self) -> str:
        return "whatsapp"

    async def resolve_internal_user_id(self, identifier: str) -> str:
        """Resolve a WhatsApp phone JID (without +) to the account's internal LID.

        Only the bot owner's own phone can be resolved this way: the LID is
        learned from the first is_from_me message after connection.  For any
        other phone number the LID is unknown until that contact sends a message,
        so the identifier is returned unchanged.
        """
        identifier = identifier.removeprefix("+").removeprefix("@")
        from neonize.utils.jid import build_jid

        try:
            _jid = build_jid(identifier)
            lid_jid = await self._client.get_lid_from_pn(_jid)
            return lid_jid.User
        except Exception:
            logger.error("Whatsapp: could not resolve identifier %r to user_id", identifier)
        return identifier

    # ── Interactive prompts (approval / ask_user) ────

    # Shaped like Telegram's approval: returns True/False. On WhatsApp there
    # are no inline buttons, so we ask the user to reply yes/no and consume
    # the next inbound message for this user.
    async def request_approval(
        self,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        if self._client is None:
            return False
        args_preview = ", ".join(f"{k}={v}" for k, v in arguments.items())
        if len(args_preview) > 200:
            args_preview = args_preview[:200] + "…"
        prompt = (
            f"🔐 *Approval required*\n\n"
            f"Tool: `{tool_name}`\n"
            f"Args: `{args_preview}`\n\n"
            f"Reply *yes* / *no*, or react with 👍 / 👎."
        )
        prompt_id: str | None = None
        try:
            prompt_id = await self._send_tracked(user_id, prompt)
        except Exception as e:
            # Can't reach the user to ask — deny cleanly so the tool call
            # surfaces as "declined by user" rather than a confusing
            # "approval callback failed" error upstream.
            logger.warning(
                "WA approval prompt undeliverable to %s for %s (%s); denying.",
                user_id,
                tool_name,
                e,
            )
            return False

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_approvals[user_id] = fut
        if prompt_id:
            self._approval_prompt_ids[prompt_id] = user_id
        try:
            return await asyncio.wait_for(fut, timeout=120)
        except TimeoutError:
            logger.info("WA approval timed out for %s / %s", user_id, tool_name)
            return False
        finally:
            self._pending_approvals.pop(user_id, None)
            if prompt_id:
                self._approval_prompt_ids.pop(prompt_id, None)

    async def request_input(self, user_id: str, question: str) -> str:
        """Prompt the user and wait for their next message as the answer."""
        if self._client is None:
            return "[WhatsApp not connected]"
        try:
            await self.send(user_id, question)
        except Exception as e:
            logger.warning("WA input prompt undeliverable to %s (%s)", user_id, e)
            return "[Could not deliver question to user]"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_inputs[user_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=120)
        except TimeoutError:
            return "[No response from user — timed out after 120s]"
        finally:
            self._pending_inputs.pop(user_id, None)

    @staticmethod
    def _parse_yes_no(text: str) -> bool | None:
        t = (text or "").strip().lower()
        if t in {"yes", "y", "ok", "okay", "sure", "approve", "approved", "allow", "👍", "✅"}:
            return True
        if t in {"no", "n", "deny", "denied", "cancel", "stop", "abort", "👎", "❌"}:
            return False
        return None

    _REACTION_APPROVE = {"👍", "✅", "👌", "❤"}
    _REACTION_DENY = {"👎", "❌", "🚫"}

    def _handle_reaction_approval(self, msg: WAMessage) -> bool:
        """If this reaction targets a pending-approval prompt, resolve it.

        Returns True if the reaction was consumed (and the caller should stop),
        False if it's an unrelated reaction that should be ignored normally.
        """
        reaction = msg.reactionMessage
        target_id = getattr(reaction.key, "ID", "") or ""
        if not target_id:
            return False
        user_id = self._approval_prompt_ids.get(target_id)
        if not user_id:
            return False
        fut = self._pending_approvals.get(user_id)
        if not fut or fut.done():
            return False

        emoji = (reaction.text or "").strip()
        if emoji in self._REACTION_APPROVE:
            fut.set_result(True)
            return True
        if emoji in self._REACTION_DENY:
            fut.set_result(False)
            return True
        # Empty string = reaction removed; ignore without consuming.
        return False

    async def _send_tracked(self, user_id: str, text: str) -> str | None:
        """Send a message and return the final chunk's WA message id (or None)."""
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")
        jid = self._jid_for(user_id)
        last_id: str | None = None
        for chunk in _split_whatsapp_message(text or ""):
            result = await self._client.send_message(jid, chunk)
            sid = getattr(result, "ID", None) if result is not None else None
            if sid is not None:
                last_id = str(sid)
                # Echo suppression: don't re-process this as an inbound message.
                while len(self._recent_sent_ids_order) >= self._max_recent_sent_ids:
                    old = self._recent_sent_ids_order.popleft()
                    self._recent_sent_ids.discard(old)
                self._recent_sent_ids_order.append(last_id)
                self._recent_sent_ids.add(last_id)
        return last_id

    # ── Progress reporting (tool_event_callback bridge) ─────

    async def notify_tool_event(
        self,
        phase: str,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
    ) -> None:
        """Surface live tool-execution progress in the user's chat.

        Maintains one in-place status bubble per user, edited on each "start".
        Also pings a COMPOSING presence so the phone shows "typing…".
        """
        if not self._client:
            return
        jid = self._jid_for(user_id)
        try:
            await self._client.send_chat_presence(
                jid,
                ChatPresence.CHAT_PRESENCE_COMPOSING,
                ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
            )
        except Exception:
            logger.debug("WA presence notify failed", exc_info=True)

        if phase != "start":
            return

        args_preview = self._format_args_preview(arguments)
        status_text = f"🟡 Running `{tool_name}`" + (f" — {args_preview}" if args_preview else "…")

        state = self._progress.get(user_id)
        if state is None:
            try:
                msg_id = await self._send_tracked(user_id, status_text)
            except Exception:
                logger.debug("WA progress send failed", exc_info=True)
                return
            if msg_id is None:
                return
            self._progress[user_id] = {
                "id": msg_id,
                "jid": jid,
                "tools": [tool_name],
            }
            return

        state["tools"].append(tool_name)
        try:
            new_msg = _WAMessageProto()
            new_msg.conversation = status_text
            await self._client.edit_message(state["jid"], state["id"], new_msg)
        except Exception:
            logger.debug("WA progress edit failed", exc_info=True)

    async def _finalize_progress(self, user_id: str) -> None:
        """Mark the status message as done once the final reply is about to land."""
        state = self._progress.pop(user_id, None)
        if not state or not self._client:
            return
        tools = state.get("tools") or []
        summary = f"✅ Done — {len(tools)} step(s)"
        try:
            new_msg = _WAMessageProto()
            new_msg.conversation = summary
            await self._client.edit_message(state["jid"], state["id"], new_msg)
        except Exception:
            logger.debug("WA progress finalize failed", exc_info=True)

    @staticmethod
    def _format_args_preview(arguments: dict[str, Any], max_len: int = 80) -> str:
        if not arguments:
            return ""
        try:
            parts = []
            for k, v in arguments.items():
                sv = str(v)
                if len(sv) > 40:
                    sv = sv[:40] + "…"
                parts.append(f"{k}={sv}")
            preview = ", ".join(parts)
        except Exception:
            return ""
        if len(preview) > max_len:
            preview = preview[:max_len] + "…"
        return preview

    # ── Lifecycle ────────────────────────────────

    _loop_started = False

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler

        # Neonize sets event_global_loop lazily inside connect() (to the
        # running asyncio loop). Older versions pre-created a separate loop
        # that required a daemon-thread runner; keep that path as a no-op
        # safety net in case a future neonize reverts. Must resolve the
        # attribute through the module — `from X import Y` captures the
        # value at import time (None), missing later reassignments.
        loop = _neonize_events.event_global_loop if _neonize_events is not None else None
        if not WhatsAppChannel._loop_started and loop is not None and not loop.is_running():
            threading.Thread(
                target=loop.run_forever,
                daemon=True,
            ).start()
            WhatsAppChannel._loop_started = True
            logger.debug("Started neonize event_global_loop in daemon thread")

        self._client = NewAClient(name=str(self._settings.data_dir / "pincer-wa.db"))

        self._client.event.qr(self._on_qr)

        self._client.event(ConnectedEv)(self._on_connected)
        self._client.event(PairStatusEv)(self._on_pair_status)
        self._client.event(MessageEv)(self._on_message)
        self._client.event(ConnectFailureEv)(self._on_connect_failure)
        self._client.event(ClientOutdatedEv)(self._on_client_outdated)
        self._client.event(LoggedOutEv)(self._on_logged_out)
        self._client.event(StreamErrorEv)(self._on_stream_error)

        neonize_log.setLevel(logging.WARNING)
        for _name in ("whatsmeow", "whatsmeow.Client", "Whatsmeow", "Whatsmeow.Database"):
            logging.getLogger(_name).setLevel(logging.CRITICAL)

        # Wrap neonize's Event.execute so exceptions from the Go callback
        # thread become visible instead of being silently swallowed.
        original_execute = self._client.event.execute

        def _safe_execute(uuid: int, binary: int, size: int, code: int) -> None:
            try:
                original_execute(uuid, binary, size, code)
            except Exception:
                logger.exception("neonize Event.execute error (code=%d)", code)

        self._client.event.execute = _safe_execute  # type: ignore[assignment]

        logger.info("Connecting to WhatsApp...")
        await self._client.connect()

        # connect() assigns neonize's event_global_loop to the running loop.
        # If a separate loop is running in our daemon thread (older neonize),
        # poke it so it picks up any task create_task() just queued.
        loop = _neonize_events.event_global_loop if _neonize_events is not None else None
        if loop is not None and loop is not asyncio.get_running_loop():
            loop.call_soon_threadsafe(lambda: None)

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=120)
        except TimeoutError:
            raise RuntimeError("WhatsApp connection timed out. Did you scan the QR code?") from None

        # Login-failure handlers set _login_error AND wake _connected so we
        # don't hang to the timeout. Surface the reason here instead.
        if self._login_error:
            raise RuntimeError(f"WhatsApp login failed: {self._login_error}")

        if loop is not None:
            logger.info(
                "event_global_loop healthy: running=%s, closed=%s",
                loop.is_running(),
                loop.is_closed(),
            )

    async def stop(self) -> None:
        if self._client:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=5.0)
            except Exception as e:
                logger.warning("WhatsApp disconnect error: %s", e)
        loop = _neonize_events.event_global_loop if _neonize_events is not None else None
        # Only stop if it's a separate loop we started in a daemon thread.
        # In current neonize, this is the running asyncio loop — stopping it
        # would kill the caller.
        if loop is not None and loop.is_running() and loop is not asyncio.get_running_loop():
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.sleep(0.2)
        logger.info("WhatsApp channel stopped")

    def _jid_for(self, user_id: str) -> Any:
        """Build a JID to send to.

        Honors, in order:
          1. An explicit "user@server" in user_id (e.g. "207855026221128@lid").
          2. A server learned from an inbound message for this user.
          3. The neonize default server (s.whatsapp.net).

        Step 2 is load-bearing for LID-based self-chats: the sender's User part
        is a LID that only resolves on "@lid", so build_jid(lid_user) defaults
        to "@s.whatsapp.net" and WA returns "no LID found".
        """
        if "@" in user_id:
            user, server = user_id.split("@", 1)
            return build_jid(user, server)
        target = self._reply_targets.get(user_id)
        if target is not None:
            user, server = target
            return build_jid(user, server)
        return build_jid(user_id)

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")

        jid = self._jid_for(user_id)
        for chunk in _split_whatsapp_message(text or ""):
            await self._client.send_message(jid, chunk)
        logger.debug("WhatsApp message sent to %s (%d chars)", user_id, len(text))

    async def send_photo(self, user_id: str, url: str, caption: str = "") -> None:
        """Send a photo (URL, local path, or bytes) inline in the chat."""
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")
        jid = self._jid_for(user_id)
        await self._client.send_image(jid, url, caption=caption or None)

    async def send_photo_from_bytes(
        self,
        user_id: str,
        data: bytes,
        mimetype: str = "image/png",
        caption: str = "",
    ) -> None:
        """Send a photo supplied as raw bytes."""
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")
        jid = self._jid_for(user_id)
        await self._client.send_image(jid, data, caption=caption or None)

    async def send_file(self, user_id: str, file_path: str, caption: str = "") -> None:
        """Send a file as a WhatsApp document."""
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")
        import mimetypes
        from pathlib import Path

        p = Path(file_path)
        filename = p.name
        mime, _ = mimetypes.guess_type(filename)
        jid = self._jid_for(user_id)
        await self._client.send_document(
            jid,
            file_path,
            caption=caption or None,
            filename=filename,
            mimetype=mime or "application/octet-stream",
        )

    async def send_animation(self, user_id: str, url: str, caption: str = "") -> None:
        """Send a GIF/animation — delivered as a looping video on WhatsApp."""
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")
        jid = self._jid_for(user_id)
        try:
            await self._client.send_video(
                jid,
                url,
                caption=caption or None,
                gifplayback=True,
                is_gif=True,
            )
        except Exception:
            logger.exception("send_animation failed, falling back to image")
            await self._client.send_image(jid, url, caption=caption or None)

    # ── Event Handlers ───────────────────────────

    async def _on_qr(self, _client: NewAClient, qr_data: bytes) -> None:
        """Handle QR code event. qr_data is the raw QR payload bytes."""
        import qrcode

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)

        f = io.StringIO()
        qr.print_ascii(out=f, invert=True)
        qr_text = f.getvalue()

        print(
            "\n"
            "========================================\n"
            "  Scan this QR code with WhatsApp\n"
            "  Settings -> Linked Devices -> Link\n"
            "========================================\n"
            f"{qr_text}"
        )

    async def _on_connected(self, client: NewAClient, _event: ConnectedEv) -> None:
        me = await client.get_me()
        self._own_jid = me.JID.User
        self._own_jid_full = Jid2String(me.JID)
        self._connected.set()
        logger.info(
            "WhatsApp connected — own_jid=%r  full_jid=%s",
            self._own_jid,
            self._own_jid_full,
        )

    async def _on_pair_status(self, _client: NewAClient, event: PairStatusEv) -> None:
        logger.info("WhatsApp paired: %s", Jid2String(event.ID))

    # ── Login-failure surface ────────────────────
    # Neonize otherwise prints a bare `Login event: <reason>` line to stdout
    # and lets start() hang to its 120s timeout. These handlers turn each
    # reason into an actionable ERROR log and unblock start() so it can raise.

    def _record_login_failure(self, reason_name: str, action: str) -> None:
        logger.error(
            "WhatsApp login failed reason=%s neonize=%s action=%s",
            reason_name,
            _NEONIZE_VERSION,
            action,
        )
        self._login_error = f"{reason_name}: {action}"
        self._connected.set()

    async def _on_connect_failure(self, _client: NewAClient, event: ConnectFailureEv) -> None:
        reason_val = int(getattr(event, "Reason", 0) or 0)
        name, action = _WA_CONNECT_FAILURE_REMEDIATION.get(
            reason_val,
            ("connect_failure", "See neonize/whatsmeow release notes; upgrade neonize first."),
        )
        self._record_login_failure(name, action)

    async def _on_client_outdated(self, _client: NewAClient, _event: ClientOutdatedEv) -> None:
        self._record_login_failure("client_outdated", _WA_CLIENT_OUTDATED_ACTION)

    async def _on_logged_out(self, _client: NewAClient, _event: LoggedOutEv) -> None:
        self._record_login_failure("logged_out", _WA_LOGGED_OUT_ACTION)

    async def _on_stream_error(self, _client: NewAClient, event: StreamErrorEv) -> None:
        code = getattr(event, "Code", "") or "unknown"
        logger.error(
            "WhatsApp stream error code=%s neonize=%s — reconnect may be required",
            code,
            _NEONIZE_VERSION,
        )

    # Skip messages older than this (seconds) — filters history-sync flood
    _MAX_MESSAGE_AGE = 120

    def _is_self_chat(self, chat_user: str) -> bool:
        """Return True when the chat is with the owner (self-chat).

        Compares against the phone-based JID, the full JID string, and
        the owner's LID (Linked Identity) if it has been learned.
        """
        if not self._own_jid:
            return False
        if chat_user == self._own_jid:
            return True
        if self._own_jid_full and chat_user in self._own_jid_full:
            return True
        return bool(self._own_lid and chat_user == self._own_lid)

    async def _on_message(self, client: NewAClient, event: MessageEv) -> None:
        """Route incoming WhatsApp messages to the handler callback."""
        try:
            info = event.Info
            msg_id = info.ID

            # Echo prevention: skip messages Pincer itself just sent.
            if msg_id and str(msg_id) in self._recent_sent_ids:
                self._recent_sent_ids.discard(str(msg_id))
                logger.debug("WA skip: echo of our own message %s", msg_id)
                return

            msg = event.Message
            source = info.MessageSource

            Jid2String(source.Sender)
            chat_jid = Jid2String(source.Chat)
            is_group = source.IsGroup
            is_from_me = source.IsFromMe
            sender_phone = source.Sender.User
            chat_user = source.Chat.User

            # Learn the owner's LID from outgoing messages.  For
            # is_from_me messages the sender is always the owner; if the
            # sender JID uses the "lid" server, its User part is the
            # owner's LID which we store for self-chat detection.
            if is_from_me and not self._own_lid:
                sender_server = source.Sender.Server
                if sender_server == "lid":
                    self._own_lid = sender_phone
                    logger.info("WA learned owner LID from sender: %s", self._own_lid)

            # Remember where to reply to this sender. Mirrors what the handler-
            # reply path at the bottom of this function uses (chat JID), so
            # approval/input prompts route to the same place.
            chat_server = source.Chat.Server
            if sender_phone and chat_user and chat_server:
                self._reply_targets[sender_phone] = (chat_user, chat_server)

            msg_type = getattr(info, "Type", None) or "unknown"
            logger.info(
                "[WA] msg in | from_me=%s group=%s chat=%s type=%s",
                is_from_me,
                is_group,
                chat_jid,
                msg_type,
            )

            # Rule 1: Ignore status broadcasts.
            if "status@broadcast" in chat_jid:
                logger.debug("WA skip: status broadcast")
                return

            # Filter out old history-sync messages.
            # Neonize may report timestamps in seconds or milliseconds.
            msg_ts = info.Timestamp.seconds if hasattr(info.Timestamp, "seconds") else int(info.Timestamp)
            if msg_ts > 1_000_000_000_000:
                msg_ts = msg_ts // 1000
            now = int(time.time())
            age = now - msg_ts
            if age > self._MAX_MESSAGE_AGE:
                logger.info("WA skip old message (age=%ds, limit=%ds)", age, self._MAX_MESSAGE_AGE)
                return

            # Rule 2: Self-chat — owner messages themselves → process.
            is_self_chat = not is_group and is_from_me and self._is_self_chat(chat_user)
            if is_self_chat:
                logger.info("WA routing: self-chat (chat_user=%s own_jid=%s)", chat_user, self._own_jid)
            else:
                # Rule 3: Outgoing to others → always ignore.
                if is_from_me:
                    logger.debug("WA skip: outgoing message to %s → ignoring", chat_jid)
                    return
                # Rule 4: Group — only process if @mentioned or trigger.
                if is_group:
                    if not self._is_mentioned_in_group(msg, client):
                        logger.debug("WA skip: group message without mention")
                        return
                    logger.info("WA routing: group mention")
                else:
                    # Rule 5: Incoming DM — only process if not self-chat-only and not a guest.
                    if self._settings.whatsapp_self_chat_only:
                        logger.info("WA skip: incoming DM from %s (self-chat-only mode)", sender_phone)
                        return
                    if (
                        self._identity is not None
                        and not self._settings.whatsapp_guests_allowed
                        and await self._identity.is_guest(ChannelType.WHATSAPP, sender_phone)
                    ):
                        logger.info("WA skip: DM from %s not in identity map (guest)", sender_phone)
                        return
                    logger.info("WA routing: DM from %s", sender_phone)

            # Defensive unwrap: the Go library should unwrap these, but
            # if for any reason the raw wrapper arrives, extract the inner
            # message so content checks find the actual text/media.
            msg = self._unwrap_message(msg)

            # Reaction-based approval: handled before the content filter,
            # which drops reactionMessage as "unsupported content".
            if msg.HasField("reactionMessage") and self._handle_reaction_approval(msg):
                return

            if not self._has_supported_content(msg):
                set_fields = self._message_set_fields(msg)
                logger.info(
                    "WA skip: no supported content; set fields: %s",
                    set_fields or "(none)",
                )
                return

            incoming = await self._extract_message(client, event, sender_phone, chat_jid, msg)
            if incoming is None:
                logger.warning(
                    "WA skip: unsupported message type from %s (check debug for message type)",
                    sender_phone,
                )
                return

            # Intercept approval/input responses before normal routing.
            approval_fut = self._pending_approvals.get(sender_phone)
            if approval_fut and not approval_fut.done():
                decision = self._parse_yes_no(incoming.text)
                if decision is None:
                    await client.send_message(
                        build_jid(chat_user, source.Chat.Server),
                        "Please reply *yes* or *no*.",
                    )
                    return
                approval_fut.set_result(decision)
                return
            input_fut = self._pending_inputs.get(sender_phone)
            if input_fut and not input_fut.done():
                input_fut.set_result(incoming.text)
                return

            # Expose both sender JID user and chat JID user as candidates.
            # On LID-based accounts sender_phone is a LID; chat_user may be
            # the phone-number JID. IdentityMiddleware tries both in order.
            if chat_user and chat_user != sender_phone:
                incoming.alt_user_ids = [chat_user]

            logger.info(
                "WhatsApp message from %s (media=%s, self_chat=%s, text=%.60r)",
                sender_phone,
                incoming.media_type or "text",
                is_self_chat,
                incoming.text,
            )

            if self._handler:
                response = await self._handler(incoming)
                await self._finalize_progress(sender_phone)
                if response:
                    reply_jid = build_jid(chat_user, source.Chat.Server)
                    for chunk in _split_whatsapp_message(response):
                        result = await client.send_message(reply_jid, chunk)
                        sent_id = getattr(result, "ID", None) if result is not None else None
                        if sent_id is not None:
                            sid = str(sent_id)
                            while len(self._recent_sent_ids_order) >= self._max_recent_sent_ids:
                                old = self._recent_sent_ids_order.popleft()
                                self._recent_sent_ids.discard(old)
                            self._recent_sent_ids_order.append(sid)
                            self._recent_sent_ids.add(sid)
                    logger.debug("WA reply sent to %s (%d chars)", chat_jid, len(response))
                else:
                    logger.debug("WA handler returned empty response")
            else:
                logger.warning("WA no handler registered")

        except Exception:
            logger.exception("WhatsApp message handler error")

    # ── Message Extraction ───────────────────────

    @staticmethod
    def _unwrap_message(msg: WAMessage) -> WAMessage:
        """Unwrap wrapper protobuf types to reach the actual content.

        Whatsmeow normally unwraps these before delivery, but as a safety
        net we handle them here in case the raw wrapper leaks through.
        """
        _wrapper_fields = (
            "deviceSentMessage",
            "ephemeralMessage",
            "viewOnceMessage",
            "viewOnceMessageV2",
            "viewOnceMessageV2Extension",
            "documentWithCaptionMessage",
            "editedMessage",
        )
        for field in _wrapper_fields:
            if msg.HasField(field):
                wrapper = getattr(msg, field)
                if wrapper.HasField("message"):
                    inner = wrapper.message
                    logger.info("WA unwrapped %s", field)
                    return WhatsAppChannel._unwrap_message(inner)
        return msg

    def _has_supported_content(self, msg: WAMessage) -> bool:
        """Return True if the message has at least one content type we handle."""
        if msg.conversation:
            return True
        if msg.HasField("extendedTextMessage"):
            return True
        if msg.HasField("imageMessage") and msg.imageMessage.mimetype:
            return True
        if msg.HasField("audioMessage") and msg.audioMessage.mimetype:
            return True
        return bool(msg.HasField("documentMessage") and msg.documentMessage.mimetype)

    @staticmethod
    def _message_set_fields(msg: WAMessage) -> list[str]:
        """Return list of Message field names that are actually set. For debug logging."""
        string_fields = ("conversation",)
        message_fields = (
            "extendedTextMessage",
            "imageMessage",
            "audioMessage",
            "documentMessage",
            "protocolMessage",
            "senderKeyDistributionMessage",
            "reactionMessage",
            "stickerMessage",
            "viewOnceMessage",
            "messageHistoryBundle",
            "messageHistoryNotice",
            "deviceSentMessage",
        )
        result = [f for f in string_fields if getattr(msg, f, "")]
        result.extend(f for f in message_fields if msg.HasField(f))
        return result

    async def _extract_message(
        self,
        client: NewAClient,
        event: MessageEv,
        sender_phone: str,
        chat_jid: str,
        msg: WAMessage | None = None,
    ) -> IncomingMessage | None:
        if msg is None:
            msg = event.Message
        msg_id = event.Info.ID

        if msg.conversation:
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.conversation,
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id=msg_id,
            )

        if msg.HasField("extendedTextMessage"):
            text = getattr(msg.extendedTextMessage, "text", None) or ""
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=text,
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id=msg_id,
            )

        if msg.HasField("imageMessage") and msg.imageMessage.mimetype:
            image_data = await client.download_any(msg)
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.imageMessage.caption or "[Image received]",
                images=[(image_data, msg.imageMessage.mimetype or "image/jpeg")],
                channel_type=ChannelType.WHATSAPP,
                media_type="image",
                media_data=image_data,
                media_mimetype=msg.imageMessage.mimetype or "image/jpeg",
            )

        if msg.HasField("audioMessage") and msg.audioMessage.mimetype:
            audio_data = await client.download_any(msg)
            transcription = await self._transcribe_audio(audio_data)
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=transcription or "[Voice note - transcription failed]",
                voice_data=audio_data,
                voice_mime=msg.audioMessage.mimetype or "audio/ogg",
                channel_type=ChannelType.WHATSAPP,
                media_type="audio",
                media_data=audio_data,
                media_mimetype=msg.audioMessage.mimetype or "audio/ogg",
                is_voice_note=True,
            )

        if msg.HasField("documentMessage") and msg.documentMessage.mimetype:
            doc_data = await client.download_any(msg)
            filename = msg.documentMessage.fileName or "document"
            mime = msg.documentMessage.mimetype or "application/octet-stream"
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=f"[Document: {filename}]",
                files=[(doc_data, mime, filename)],
                channel_type=ChannelType.WHATSAPP,
                media_type="document",
                media_data=doc_data,
                media_mimetype=mime,
                media_filename=filename,
            )

        set_fields = self._message_set_fields(msg)
        logger.debug(
            "WA extract skipped: no supported content (set fields: %s) from %s",
            set_fields or "(none)",
            sender_phone,
        )
        return None

    async def _transcribe_audio(self, audio_data: bytes) -> str | None:
        """Transcribe voice note using the existing Whisper tool."""
        try:
            from pincer.tools.builtin.transcribe import transcribe_voice

            api_key = self._settings.openai_api_key.get_secret_value()
            if not api_key:
                return None
            return await transcribe_voice(audio_data, "audio/ogg", api_key)
        except ImportError:
            logger.warning("Transcription unavailable: transcribe module not found")
            return None
        except Exception as e:
            logger.error("Transcription failed: %s", e)
            return None

    # ── Group Mention Detection ──────────────────

    def _is_mentioned_in_group(self, msg: WAMessage, client: NewAClient) -> bool:
        own_jid = self._own_jid

        if msg.HasField("extendedTextMessage") and msg.extendedTextMessage.HasField("contextInfo"):
            ctx = msg.extendedTextMessage.contextInfo
            if ctx.mentionedJID:
                for jid in ctx.mentionedJID:
                    if own_jid and own_jid in str(jid):
                        return True

        text = msg.conversation or ""
        if msg.HasField("extendedTextMessage"):
            text = msg.extendedTextMessage.text or ""

        if own_jid and own_jid in text:
            return True

        trigger = self._settings.whatsapp_group_trigger
        return bool(trigger and trigger.lower() in text.lower())
