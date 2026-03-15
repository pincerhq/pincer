"""Abstract channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    """Supported communication channels."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    CLI = "cli"
    WEB = "web"
    VOICE = "voice"
    SIGNAL = "signal"


@dataclass
class IncomingMessage:
    """Unified incoming message from any channel."""

    user_id: str
    channel: str
    text: str = ""
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (raw_bytes, media_type)
    files: list[tuple[bytes, str, str]] = field(default_factory=list)  # (raw_bytes, mime, filename)
    voice_data: bytes | None = None
    voice_mime: str = ""
    reply_to_message_id: str | None = None
    raw: Any = None

    # Sprint 3: cross-channel identity
    pincer_user_id: str = ""
    channel_type: ChannelType = ChannelType.TELEGRAM

    # Sprint 3: generic media fields (used by WhatsApp)
    media_type: str | None = None  # "image", "audio", "document", None
    media_data: bytes | None = None
    media_mimetype: str | None = None
    media_filename: str | None = None
    is_voice_note: bool = False

    @property
    def has_voice(self) -> bool:
        return self.voice_data is not None

    @property
    def has_files(self) -> bool:
        return len(self.files) > 0


# Type for the callback the channel calls when a message arrives
MessageHandler = Callable[[IncomingMessage], Awaitable[str]]


class BaseChannel(ABC):
    """Abstract messaging channel."""

    channel_type: ChannelType = ChannelType.TELEGRAM

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel identifier (e.g. 'telegram', 'whatsapp')."""
        ...

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """Start listening for messages."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and clean up."""
        ...

    @abstractmethod
    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a message to a user."""
        ...

    async def send_file(
        self, user_id: str, file_path: str, caption: str = ""
    ) -> None:
        """Send a file to a user. Default implementation sends a text link."""
        await self.send(user_id, f"[File: {file_path}] {caption}".strip())

    async def send_photo(
        self, user_id: str, url: str, caption: str = ""
    ) -> None:
        """Send a photo from a URL. Default implementation sends a text link."""
        await self.send(user_id, f"{caption}\n{url}".strip())

    async def send_animation(
        self, user_id: str, url: str, caption: str = ""
    ) -> None:
        """Send an animation/GIF from a URL. Default implementation sends a text link."""
        await self.send(user_id, f"{caption}\n{url}".strip())

    async def send_streaming(
        self, user_id: str, chunks: AsyncIterator[str]
    ) -> None:
        """Send a streaming response by progressively updating the message.
        Default implementation collects all chunks and sends as one message."""
        full = ""
        async for chunk in chunks:
            full += chunk
        if full:
            await self.send(user_id, full)
