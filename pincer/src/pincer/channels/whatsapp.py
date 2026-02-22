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
from typing import TYPE_CHECKING, Any

try:
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import (
        ConnectedEv,
        MessageEv,
        PairStatusEv,
        QREvent,
    )
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        Message as WAMessage,
    )
    from neonize.utils import build_jid, log as neonize_log

    HAS_NEONIZE = True
except ImportError:
    HAS_NEONIZE = False
    NewAClient = None  # type: ignore[assignment,misc]
    neonize_log = None  # type: ignore[assignment]

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

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        db_path = str(self._settings.data_dir / "whatsapp.db")
        self._client = NewAClient(name="pincer-wa", database=db_path)

        self._client.event(self._on_qr)
        self._client.event(self._on_connected)
        self._client.event(self._on_pair_status)
        self._client.event(self._on_message)

        neonize_log.setLevel(logging.WARNING)

        logger.info("Connecting to WhatsApp...")
        await self._client.connect()

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "WhatsApp connection timed out. Did you scan the QR code?"
            ) from None

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
        await self._client.send_message(jid, text=text)
        logger.debug("WhatsApp message sent to %s (%d chars)", user_id, len(text))

    # ── Event Handlers ───────────────────────────

    async def _on_qr(self, _client: NewAClient, event: QREvent) -> None:
        import qrcode

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1,
        )
        qr.add_data(event.qr)
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
        self._own_jid = str(client.get_me().user)
        self._connected.set()
        logger.info("WhatsApp connected as %s", self._own_jid)

    async def _on_pair_status(self, _client: NewAClient, event: PairStatusEv) -> None:
        logger.info("WhatsApp paired: %s", event.id)

    async def _on_message(self, client: NewAClient, event: MessageEv) -> None:
        """Route incoming WhatsApp messages to the handler callback."""
        try:
            msg = event.message
            info = event.info

            sender_jid = str(info.message_source.sender)
            chat_jid = str(info.message_source.chat)
            is_group = info.message_source.is_group
            is_from_me = info.message_source.is_from_me

            sender_phone = sender_jid.split("@")[0]

            is_self_chat = (
                not is_group
                and chat_jid.split("@")[0] == self._own_jid
                and is_from_me
            )

            if is_self_chat:
                pass  # Process as agent command
            elif is_group:
                if not self._is_mentioned_in_group(msg, client):
                    return
            elif not is_from_me:
                if self._dm_allowlist and sender_phone not in self._dm_allowlist:
                    logger.debug("WhatsApp DM blocked: %s (not in allowlist)", sender_phone)
                    return
            elif is_from_me and not is_self_chat:
                return  # Our own reply in non-self-chat

            incoming = await self._extract_message(client, event, sender_phone, chat_jid)
            if incoming is None:
                return

            logger.info(
                "WhatsApp message from %s (media=%s, self_chat=%s)",
                sender_phone,
                incoming.media_type or "text",
                is_self_chat,
            )

            if self._handler:
                response = await self._handler(incoming)
                if response:
                    reply_jid = build_jid(chat_jid.split("@")[0])
                    await client.send_message(reply_jid, text=response)

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
        msg = event.message
        msg_id = str(event.info.id)

        if msg.conversation:
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.conversation,
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id=msg_id,
            )

        if msg.extended_text_message:
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.extended_text_message.text or "",
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id=msg_id,
            )

        if msg.image_message:
            image_data = await client.download_any(msg)
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=msg.image_message.caption or "[Image received]",
                images=[(image_data, msg.image_message.mimetype or "image/jpeg")],
                channel_type=ChannelType.WHATSAPP,
                media_type="image",
                media_data=image_data,
                media_mimetype=msg.image_message.mimetype or "image/jpeg",
            )

        if msg.audio_message:
            audio_data = await client.download_any(msg)
            transcription = await self._transcribe_audio(audio_data)
            return IncomingMessage(
                user_id=sender_phone,
                channel="whatsapp",
                text=transcription or "[Voice note - transcription failed]",
                voice_data=audio_data,
                voice_mime=msg.audio_message.mimetype or "audio/ogg",
                channel_type=ChannelType.WHATSAPP,
                media_type="audio",
                media_data=audio_data,
                media_mimetype=msg.audio_message.mimetype or "audio/ogg",
                is_voice_note=True,
            )

        if msg.document_message:
            doc_data = await client.download_any(msg)
            filename = msg.document_message.file_name or "document"
            mime = msg.document_message.mimetype or "application/octet-stream"
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

        if msg.extended_text_message and msg.extended_text_message.context_info:
            ctx = msg.extended_text_message.context_info
            if ctx.mentioned_jid:
                for jid in ctx.mentioned_jid:
                    if own_jid and own_jid in str(jid):
                        return True

        text = msg.conversation or ""
        if msg.extended_text_message:
            text = msg.extended_text_message.text or ""

        if own_jid and own_jid in text:
            return True

        trigger = self._settings.whatsapp_group_trigger
        if trigger and trigger.lower() in text.lower():
            return True

        return False
