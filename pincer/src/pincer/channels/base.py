"""Abstract channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncomingMessage:
    """Unified incoming message from any channel."""

    user_id: str
    channel: str
    text: str = ""
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (raw_bytes, media_type)
    voice_data: bytes | None = None
    voice_mime: str = ""
    reply_to_message_id: str | None = None
    raw: Any = None  # Original platform message object

    @property
    def has_voice(self) -> bool:
        return self.voice_data is not None


# Type for the callback the channel calls when a message arrives
MessageHandler = Callable[[IncomingMessage], Awaitable[str]]


class BaseChannel(ABC):
    """Abstract messaging channel."""

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
