"""Signal messenger channel via signal-cli-rest-api."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage

if TYPE_CHECKING:
    from pincer.channels.base import MessageHandler
    from pincer.channels.signal_client import SignalClient
    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LEN = 6000


def _split_message(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Split long text into chunks that fit within Signal's message limit."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


class SignalChannel(BaseChannel):
    """Signal messenger channel."""

    channel_type = ChannelType.SIGNAL

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: SignalClient | None = None
        self._handler: MessageHandler | None = None
        self._identity: IdentityResolver | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._seen: set[int] = set()  # deduplicate by timestamp

    @property
    def name(self) -> str:
        return "signal"

    def set_identity_resolver(self, resolver: IdentityResolver) -> None:
        self._identity = resolver

    async def start(self, handler: MessageHandler) -> None:
        from pincer.channels.signal_client import SignalAPIError, SignalClient

        self._handler = handler
        self._client = SignalClient(
            base_url=self._settings.signal_api_url,
            phone_number=self._settings.signal_phone_number,
        )
        await self._client.connect()

        # Verify the API is reachable
        try:
            await self._client.health()
        except SignalAPIError as exc:
            logger.warning("Signal API health check failed: %s", exc)

        mode = getattr(self._settings, "signal_receive_mode", "websocket")
        if mode == "websocket":
            task = asyncio.create_task(self._websocket_loop())
        else:
            task = asyncio.create_task(self._poll_loop())
        self._tasks.append(task)
        logger.info("SignalChannel started (mode=%s)", mode)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._client:
            await self._client.disconnect()
            self._client = None
        logger.info("SignalChannel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        if not self._client:
            logger.error("SignalChannel.send called before start()")
            return

        is_group: bool = kwargs.get("is_group", False)
        group_id: str = kwargs.get("group_id", "")
        recipient: str = kwargs.get("recipient", user_id)

        # Typing indicator (best-effort)
        if not is_group:
            with contextlib.suppress(Exception):
                await self._client.send_typing_indicator(recipient)

        for chunk in _split_message(text):
            try:
                if is_group and group_id:
                    await self._client.send_group_message(group_id, chunk)
                else:
                    await self._client.send_message(recipient, chunk)
            except Exception as exc:
                logger.error("Signal send failed: %s", exc)

    # ── Receive loops ─────────────────────────────────────────────────────────

    async def _websocket_loop(self) -> None:
        from pincer.channels.signal_client import SignalAPIError

        backoff = 1
        while True:
            try:
                assert self._client is not None
                async for msg in self._client.websocket_receive():
                    backoff = 1
                    await self._process_signal_message(msg)
            except asyncio.CancelledError:
                return
            except SignalAPIError as exc:
                logger.warning("Signal WS error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.error("Signal WS unexpected error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll_loop(self) -> None:
        from pincer.channels.signal_client import SignalAPIError

        interval = getattr(self._settings, "signal_poll_interval", 2)
        while True:
            try:
                await asyncio.sleep(interval)
                assert self._client is not None
                messages = await self._client.receive()
                for msg in messages:
                    await self._process_signal_message(msg)
            except asyncio.CancelledError:
                return
            except SignalAPIError as exc:
                logger.warning("Signal poll error: %s", exc)
            except Exception as exc:
                logger.error("Signal poll unexpected error: %s", exc)

    # ── Message processing ────────────────────────────────────────────────────

    async def _process_signal_message(self, msg: Any) -> None:  # SignalMessage
        if not self._handler or not self._client:
            return

        # Dedup by timestamp
        if msg.timestamp and msg.timestamp in self._seen:
            return
        if msg.timestamp:
            self._seen.add(msg.timestamp)
            # Keep set bounded
            if len(self._seen) > 10000:
                self._seen = set(list(self._seen)[-5000:])

        # DM allowlist check
        allowlist_raw: str = getattr(self._settings, "signal_allowlist", "")
        if not msg.is_group and allowlist_raw.strip():
            allowed = {p.strip().lstrip("+") for p in allowlist_raw.split(",") if p.strip()}
            sender_clean = msg.source.lstrip("+")
            if sender_clean not in allowed:
                logger.debug("Signal: DM from %s not in allowlist — ignored", msg.source)
                return

        # Group mention check
        if msg.is_group:
            group_reply = getattr(self._settings, "signal_group_reply", "mention_only")
            if group_reply == "disabled":
                return
            if group_reply == "mention_only":
                agent_name = self._settings.agent_name.lower()
                own_phone = self._settings.signal_phone_number.lstrip("+")
                text_lower = msg.text.lower()
                if agent_name not in text_lower and own_phone not in text_lower:
                    return

        text = msg.text or ""

        # Voice note transcription
        if msg.has_voice and msg.attachments:
            for att in msg.attachments:
                if att.content_type.startswith("audio/") and att.id:
                    try:
                        audio_data = await self._client.get_attachment(att.id)
                        from pincer.tools.builtin.transcribe import transcribe_voice

                        openai_key = self._settings.openai_api_key.get_secret_value()
                        transcribed = await transcribe_voice(
                            audio_data, att.content_type, openai_key
                        )
                        text = f"[Voice note]: {transcribed}" if transcribed else text
                    except Exception as exc:
                        logger.warning("Voice transcription failed: %s", exc)
                    break

        # Identity resolution
        user_id = msg.source
        if self._identity:
            try:
                user_id = await self._identity.resolve(
                    ChannelType.SIGNAL, msg.source, msg.source_name or None
                )
            except Exception as exc:
                logger.warning("Identity resolution failed: %s", exc)

        incoming = IncomingMessage(
            user_id=user_id,
            channel=self.name,
            text=text,
            pincer_user_id=user_id,
            channel_type=ChannelType.SIGNAL,
            raw=msg,
        )

        try:
            reply = await self._handler(incoming)
        except Exception as exc:
            logger.error("Message handler error: %s", exc)
            return

        if reply:
            await self.send(
                user_id=msg.source,
                text=reply,
                is_group=msg.is_group,
                group_id=msg.group_id,
                recipient=msg.source,
            )
