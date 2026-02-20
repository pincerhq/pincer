"""
Telegram channel implementation using aiogram 3.x.

Features:
- Text messages, voice notes, images, documents
- Typing indicator while agent thinks
- User allowlist
- Long message splitting at paragraph boundaries
- Markdown formatting
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart

from pincer.channels.base import BaseChannel, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiogram.types import Message

    from pincer.config import Settings

logger = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_LENGTH = 4096


def split_message(text: str, max_len: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    """Split long text at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_len:
            if current:
                chunks.append(current.strip())
                current = ""
            # If single paragraph exceeds max, split by lines
            if len(paragraph) > max_len:
                for line in paragraph.split("\n"):
                    if len(current) + len(line) + 1 > max_len:
                        if current:
                            chunks.append(current.strip())
                        current = line + "\n"
                    else:
                        current += line + "\n"
            else:
                current = paragraph + "\n\n"
        else:
            current += paragraph + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text[:max_len]]


class TelegramChannel(BaseChannel):
    """Telegram bot channel."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._handler: MessageHandler | None = None
        self._stream_agent: Any = None
        self._allowed_users = set(settings.telegram_allowed_users)
        self._polling_task: asyncio.Task[None] | None = None

    def set_stream_agent(self, agent: Any) -> None:
        """Set the Agent instance for streaming support."""
        self._stream_agent = agent

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        token = self._settings.telegram_bot_token.get_secret_value()
        if not token:
            raise ValueError("PINCER_TELEGRAM_BOT_TOKEN is required")

        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self._dp = Dispatcher()

        router = Router()
        self._register_handlers(router)
        self._dp.include_router(router)

        logger.info("Starting Telegram polling...")
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False)
        )

    async def stop(self) -> None:
        if self._dp:
            await self._dp.stop_polling()
        if self._bot:
            await self._bot.session.close()
        if self._polling_task:
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
        logger.info("Telegram channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        assert self._bot is not None
        for chunk in split_message(text):
            try:
                await self._bot.send_message(
                    chat_id=int(user_id),
                    text=chunk,
                )
            except Exception as e:
                logger.warning("Markdown send failed, retrying plain: %s", e)
                await self._bot.send_message(
                    chat_id=int(user_id),
                    text=chunk,
                    parse_mode=None,
                )

    async def send_streaming(
        self, user_id: str, chunks: AsyncIterator[str]
    ) -> None:
        """Send a streaming response, editing the message as chunks arrive."""
        assert self._bot is not None
        chat_id = int(user_id)

        msg = await self._bot.send_message(chat_id=chat_id, text="...", parse_mode=None)
        buffer = ""
        last_edit = time.monotonic()
        edit_interval = 1.5  # Telegram rate-limits edits to ~30/min per chat

        async for chunk in chunks:
            buffer += chunk
            now = time.monotonic()
            if now - last_edit >= edit_interval and len(buffer) > 3:
                with contextlib.suppress(Exception):
                    await msg.edit_text(buffer, parse_mode=None)
                last_edit = now

        # Final edit with full text + markdown
        if buffer:
            for text_chunk in split_message(buffer):
                try:
                    await msg.edit_text(text_chunk)
                except Exception:
                    with contextlib.suppress(Exception):
                        await msg.edit_text(text_chunk, parse_mode=None)

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True  # Empty = allow all
        return user_id in self._allowed_users

    def _register_handlers(self, router: Router) -> None:
        """Register all message handlers on the router."""

        @router.message(CommandStart())
        async def cmd_start(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            await message.answer(
                f"*{self._settings.agent_name}* is ready!\n\n"
                "I'm your personal AI agent. Send me a message, voice note, or photo.\n\n"
                "Commands:\n"
                "/clear — Reset conversation\n"
                "/cost — Today's API spend\n"
                "/help — Show this message"
            )

        @router.message(Command("clear"))
        async def cmd_clear(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            if self._handler:
                await self._handler(
                    IncomingMessage(
                        user_id=str(message.from_user.id),
                        channel="telegram",
                        text="/clear",
                        raw=message,
                    )
                )
            await message.answer("Conversation cleared.")

        @router.message(Command("cost"))
        async def cmd_cost(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            if self._handler:
                response = await self._handler(
                    IncomingMessage(
                        user_id=str(message.from_user.id),
                        channel="telegram",
                        text="/cost",
                        raw=message,
                    )
                )
                await message.answer(response)

        @router.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            if not message.from_user:
                return
            await message.answer(
                "*Pincer Help*\n\n"
                "Just send me a message and I'll help!\n\n"
                "*I can:*\n"
                "- Answer questions & chat\n"
                "- Search the web\n"
                "- Run shell commands (with approval)\n"
                "- Read & write files\n"
                "- Process voice notes & images\n\n"
                "*Commands:*\n"
                "/clear — Reset conversation\n"
                "/cost — Today's API spend\n"
                "/help — This message"
            )

        @router.message(F.voice)
        async def handle_voice(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            assert self._bot is not None and self._handler is not None

            await self._bot.send_chat_action(message.chat.id, ChatAction.TYPING)

            voice = message.voice
            assert voice is not None
            file = await self._bot.get_file(voice.file_id)
            assert file.file_path is not None
            data = io.BytesIO()
            await self._bot.download_file(file.file_path, data)

            incoming = IncomingMessage(
                user_id=str(message.from_user.id),
                channel="telegram",
                text=message.caption or "",
                voice_data=data.getvalue(),
                voice_mime=voice.mime_type or "audio/ogg",
                raw=message,
            )

            response = await self._handler(incoming)
            for chunk in split_message(response):
                await message.answer(chunk)

        @router.message(F.photo)
        async def handle_photo(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            assert self._bot is not None and self._handler is not None

            await self._bot.send_chat_action(message.chat.id, ChatAction.TYPING)

            photo = message.photo[-1] if message.photo else None
            if not photo:
                return

            file = await self._bot.get_file(photo.file_id)
            assert file.file_path is not None
            data = io.BytesIO()
            await self._bot.download_file(file.file_path, data)

            incoming = IncomingMessage(
                user_id=str(message.from_user.id),
                channel="telegram",
                text=message.caption or "What's in this image?",
                images=[(data.getvalue(), "image/jpeg")],
                raw=message,
            )

            response = await self._handler(incoming)
            for chunk in split_message(response):
                await message.answer(chunk)

        @router.message(F.text)
        async def handle_text(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            if not message.text:
                return
            assert self._bot is not None and self._handler is not None

            await self._bot.send_chat_action(message.chat.id, ChatAction.TYPING)

            user_id = str(message.from_user.id)

            # Use streaming if agent is available
            if self._stream_agent is not None:
                from pincer.core.agent import StreamEventType

                async def text_chunks() -> AsyncIterator[str]:
                    async for chunk in self._stream_agent.handle_message_stream(
                        user_id=user_id,
                        channel="telegram",
                        text=message.text,
                    ):
                        if chunk.type == StreamEventType.TEXT:
                            yield chunk.content
                        elif chunk.type == StreamEventType.TOOL_START:
                            yield f"\n[{chunk.content}]\n"

                await self.send_streaming(user_id, text_chunks())
                return

            incoming = IncomingMessage(
                user_id=user_id,
                channel="telegram",
                text=message.text,
                raw=message,
            )

            response = await self._handler(incoming)
            for chunk in split_message(response):
                await message.answer(chunk)
