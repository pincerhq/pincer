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
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import (
        ConnectedEv,
        MessageEv,
        PairStatusEv,
        event_global_loop,
    )
    from neonize.utils import Jid2String, build_jid
    from neonize.utils import log as neonize_log

    HAS_NEONIZE = True
except ImportError:
    HAS_NEONIZE = False
    NewAClient = None  # type: ignore[assignment,misc]
    neonize_log = None  # type: ignore[assignment]
    event_global_loop = None  # type: ignore[assignment]

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

logger = logging.getLogger(__name__)


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel using neonize (whatsmeow Go backend)."""

    channel_type = ChannelType.WHATSAPP

    def __init__(self, settings: Settings) -> None:
        if not HAS_NEONIZE:
            raise ImportError(
                "neonize is required for WhatsApp support. "
                "Install it with: pip install neonize (requires libmagic)"
            )
        self._settings = settings
        self._client: NewAClient | None = None
        self._handler: MessageHandler | None = None
        self._own_jid: str | None = None
        self._own_jid_full: str | None = None
        self._own_lid: str | None = None
        self._connected = asyncio.Event()

        self._dm_allowlist: set[str] = set()
        if settings.whatsapp_dm_allowlist:
            self._dm_allowlist = {
                phone.strip().lstrip("+")
                for phone in settings.whatsapp_dm_allowlist.split(",")
                if phone.strip()
            }
            logger.info("WhatsApp allowlist: %d numbers", len(self._dm_allowlist))

        # Echo prevention: skip processing messages Pincer just sent.
        self._recent_sent_ids: set[str] = set()
        self._recent_sent_ids_order: deque[str] = deque()
        self._max_recent_sent_ids = 100

    @property
    def name(self) -> str:
        return "whatsapp"

    # ── Lifecycle ────────────────────────────────

    _loop_started = False

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler

        # Neonize dispatches all event callbacks via
        # asyncio.run_coroutine_threadsafe(..., event_global_loop) but never
        # starts that loop.  We run it in a daemon thread so callbacks fire.
        if (
            not WhatsAppChannel._loop_started
            and event_global_loop is not None
            and not event_global_loop.is_running()
        ):
            threading.Thread(
                target=event_global_loop.run_forever,
                daemon=True,
            ).start()
            WhatsAppChannel._loop_started = True
            logger.debug("Started neonize event_global_loop in daemon thread")

        self._client = NewAClient(name="pincer-wa")

        self._client.event.qr(self._on_qr)

        self._client.event(ConnectedEv)(self._on_connected)
        self._client.event(PairStatusEv)(self._on_pair_status)
        self._client.event(MessageEv)(self._on_message)

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

        # connect() schedules the Go backend coroutine on event_global_loop via
        # create_task(), but that call from the main thread doesn't wake the
        # daemon loop's I/O selector.  Poke it so it picks up the queued task.
        event_global_loop.call_soon_threadsafe(lambda: None)

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=120)
        except TimeoutError:
            raise RuntimeError(
                "WhatsApp connection timed out. Did you scan the QR code?"
            ) from None

        logger.info(
            "event_global_loop healthy: running=%s, closed=%s",
            event_global_loop.is_running(),
            event_global_loop.is_closed(),
        )

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning("WhatsApp disconnect error: %s", e)
        if event_global_loop is not None and event_global_loop.is_running():
            event_global_loop.call_soon_threadsafe(event_global_loop.stop)
            await asyncio.sleep(0.2)
        logger.info("WhatsApp channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        if not self._client:
            from pincer.exceptions import ChannelNotConnectedError

            raise ChannelNotConnectedError("WhatsApp client not connected")

        jid = build_jid(user_id.split("@")[0])
        await self._client.send_message(jid, text)
        logger.debug("WhatsApp message sent to %s (%d chars)", user_id, len(text))

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
                    # Rule 5: Incoming DM — only process if allowlisted (and not self-chat-only).
                    if self._settings.whatsapp_self_chat_only:
                        logger.info("WA skip: incoming DM from %s (self-chat-only mode)", sender_phone)
                        return
                    if sender_phone not in self._dm_allowlist:
                        logger.info("WA skip: DM from %s not in allowlist", sender_phone)
                        return
                    logger.info("WA routing: DM from %s", sender_phone)

            # Defensive unwrap: the Go library should unwrap these, but
            # if for any reason the raw wrapper arrives, extract the inner
            # message so content checks find the actual text/media.
            msg = self._unwrap_message(msg)

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

            logger.info(
                "WhatsApp message from %s (media=%s, self_chat=%s, text=%.60r)",
                sender_phone,
                incoming.media_type or "text",
                is_self_chat,
                incoming.text,
            )

            if self._handler:
                response = await self._handler(incoming)
                if response:
                    reply_jid = build_jid(chat_user, source.Chat.Server)
                    result = await client.send_message(reply_jid, response)
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
