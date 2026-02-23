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
import time
import threading
from typing import TYPE_CHECKING, Any

try:
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import (
        ConnectedEv,
        MessageEv,
        PairStatusEv,
        QREv,
        event_global_loop,
    )
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        Message as WAMessage,
    )
    from neonize.utils import build_jid, Jid2String, log as neonize_log

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
        self._connected = asyncio.Event()

        self._dm_allowlist: set[str] = set()
        if settings.whatsapp_dm_allowlist:
            self._dm_allowlist = {
                phone.strip().lstrip("+")
                for phone in settings.whatsapp_dm_allowlist.split(",")
                if phone.strip()
            }
            logger.info("WhatsApp allowlist: %d numbers", len(self._dm_allowlist))

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
        if not WhatsAppChannel._loop_started and event_global_loop is not None:
            if not event_global_loop.is_running():
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
        except asyncio.TimeoutError:
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
        self._connected.set()
        logger.info(
            "WhatsApp connected — own_jid=%r  full_jid=%s",
            self._own_jid,
            Jid2String(me.JID),
        )

    async def _on_pair_status(self, _client: NewAClient, event: PairStatusEv) -> None:
        logger.info("WhatsApp paired: %s", Jid2String(event.ID))

    # Skip messages older than this (seconds) — filters history-sync flood
    _MAX_MESSAGE_AGE = 120

    async def _on_message(self, client: NewAClient, event: MessageEv) -> None:
        """Route incoming WhatsApp messages to the handler callback."""
        try:
            msg = event.Message
            info = event.Info
            source = info.MessageSource

            sender_jid = Jid2String(source.Sender)
            chat_jid = Jid2String(source.Chat)
            is_group = source.IsGroup
            is_from_me = source.IsFromMe
            sender_phone = source.Sender.User
            chat_user = source.Chat.User

            logger.info(
                "WA event: sender=%s chat=%s chat_user=%r own_jid=%r "
                "from_me=%s group=%s ts=%s",
                sender_phone,
                chat_jid,
                chat_user,
                self._own_jid,
                is_from_me,
                is_group,
                info.Timestamp,
            )

            # Filter out old history-sync messages.
            # Neonize may report timestamps in seconds or milliseconds.
            msg_ts = info.Timestamp.seconds if hasattr(info.Timestamp, "seconds") else int(info.Timestamp)
            if msg_ts > 1_000_000_000_000:
                msg_ts = msg_ts // 1000
            now = int(time.time())
            age = now - msg_ts
            if age > self._MAX_MESSAGE_AGE:
                logger.debug("WA skip old message (age=%ds, limit=%ds)", age, self._MAX_MESSAGE_AGE)
                return

            # Self-chat: any non-group message flagged is_from_me.
            # WhatsApp may use a LID (Linked Identity) instead of the phone
            # number as chat_user, so we cannot rely on chat_user == own_jid.
            is_self_chat = not is_group and is_from_me

            if is_self_chat:
                logger.debug("WA routing: self-chat")
            elif is_group:
                if not self._is_mentioned_in_group(msg, client):
                    logger.debug("WA skip: group message without mention")
                    return
                logger.debug("WA routing: group mention")
            elif not is_from_me:
                if self._dm_allowlist and sender_phone not in self._dm_allowlist:
                    logger.debug("WA skip: DM from %s not in allowlist %s", sender_phone, self._dm_allowlist)
                    return
                logger.debug("WA routing: DM from %s", sender_phone)

            incoming = await self._extract_message(client, event, sender_phone, chat_jid)
            if incoming is None:
                logger.warning("WA skip: unsupported message type from %s", sender_phone)
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
                    await client.send_message(reply_jid, response)
                    logger.debug("WA reply sent to %s (%d chars)", chat_jid, len(response))
                else:
                    logger.debug("WA handler returned empty response")
            else:
                logger.warning("WA no handler registered")

        except Exception:
            logger.exception("WhatsApp message handler error")

    # ── Message Extraction ───────────────────────

    async def _extract_message(
        self,
        client: NewAClient,
        event: MessageEv,
        sender_phone: str,
        chat_jid: str,
    ) -> IncomingMessage | None:
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

        if msg.extendedTextMessage and msg.extendedTextMessage.text:
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.extendedTextMessage.text,
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id=msg_id,
            )

        if msg.imageMessage and msg.imageMessage.mimetype:
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

        if msg.audioMessage and msg.audioMessage.mimetype:
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

        if msg.documentMessage and msg.documentMessage.mimetype:
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

        logger.debug("Unsupported WhatsApp message type from %s", sender_phone)
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

        if msg.extendedTextMessage and msg.extendedTextMessage.contextInfo:
            ctx = msg.extendedTextMessage.contextInfo
            if ctx.mentionedJID:
                for jid in ctx.mentionedJID:
                    if own_jid and own_jid in str(jid):
                        return True

        text = msg.conversation or ""
        if msg.extendedTextMessage:
            text = msg.extendedTextMessage.text or ""

        if own_jid and own_jid in text:
            return True

        trigger = self._settings.whatsapp_group_trigger
        if trigger and trigger.lower() in text.lower():
            return True

        return False
