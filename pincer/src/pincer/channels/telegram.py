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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiogram.types import Message

    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

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

    channel_type = ChannelType.TELEGRAM

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._handler: MessageHandler | None = None
        self._stream_agent: Any = None
        self._allowed_users = set(settings.telegram_allowed_users)
        self._polling_task: asyncio.Task[None] | None = None
        self._identity: IdentityResolver | None = None
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}

    def set_identity_resolver(self, identity: IdentityResolver) -> None:
        """Set the identity resolver for cross-channel user mapping."""
        self._identity = identity

    def set_stream_agent(self, agent: Any) -> None:
        """Set the Agent instance for streaming support."""
        self._stream_agent = agent

    async def request_approval(
        self, user_id: str, tool_name: str, arguments: dict[str, Any],
    ) -> bool:
        """Send an inline-keyboard approval prompt and block until the user responds."""
        assert self._bot is not None

        args_preview = ", ".join(f"{k}={v}" for k, v in arguments.items())
        if len(args_preview) > 200:
            args_preview = args_preview[:200] + "…"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", callback_data=f"tool_approve:{user_id}"),
            InlineKeyboardButton(text="Deny", callback_data=f"tool_deny:{user_id}"),
        ]])

        await self._bot.send_message(
            chat_id=int(user_id),
            text=(
                f"*Approval required*\n\n"
                f"Tool: `{tool_name}`\n"
                f"Args: `{args_preview}`\n\n"
                f"Allow this action?"
            ),
            reply_markup=keyboard,
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending_approvals[user_id] = future

        try:
            return await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            logger.info("Approval timed out for user %s, tool %s", user_id, tool_name)
            return False
        finally:
            self._pending_approvals.pop(user_id, None)

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

    async def send_file(
        self, user_id: str, file_path: str, caption: str = ""
    ) -> None:
        """Send a file as a Telegram document."""
        assert self._bot is not None
        from aiogram.types import FSInputFile

        try:
            doc = FSInputFile(file_path)
            await self._bot.send_document(
                chat_id=int(user_id),
                document=doc,
                caption=caption or None,
            )
        except Exception:
            logger.exception("Failed to send file %s", file_path)
            await self.send(user_id, f"Failed to send file: {file_path}")

    async def _download_image(self, url: str) -> bytes:
        """Download image bytes with a browser-like User-Agent."""
        import aiohttp

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} fetching {url}")
                ct = resp.content_type or ""
                if not ct.startswith("image/") and "octet-stream" not in ct:
                    raise RuntimeError(f"Not an image (content-type: {ct})")
                return await resp.read()

    async def send_photo(
        self, user_id: str, url: str, caption: str = ""
    ) -> None:
        """Send a photo from a URL inline in the chat.

        Tries three strategies in order:
        1. Pass the raw URL string to Telegram (works for publicly accessible images)
        2. Download with browser headers and send as BufferedInputFile
        Raises on failure so the caller can report the error to the LLM.
        """
        assert self._bot is not None
        from aiogram.types import BufferedInputFile

        chat_id = int(user_id)

        # Fast path: let Telegram fetch the URL directly
        try:
            await self._bot.send_photo(chat_id=chat_id, photo=url, caption=caption or None)
            return
        except Exception:
            logger.debug("Telegram couldn't fetch URL directly, downloading ourselves: %s", url)

        # Slow path: download ourselves with browser headers
        data = await self._download_image(url)
        ext = url.rsplit(".", 1)[-1].lower() if "." in url else "jpg"
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        photo = BufferedInputFile(data, filename=f"image.{ext}")
        await self._bot.send_photo(chat_id=chat_id, photo=photo, caption=caption or None)

    async def send_animation(
        self, user_id: str, url: str, caption: str = ""
    ) -> None:
        """Send a GIF/animation from a URL inline in the chat.

        Same strategy as send_photo: try raw URL, then download + BufferedInputFile.
        Raises on failure so the caller can report the error to the LLM.
        """
        assert self._bot is not None
        from aiogram.types import BufferedInputFile

        chat_id = int(user_id)

        try:
            await self._bot.send_animation(chat_id=chat_id, animation=url, caption=caption or None)
            return
        except Exception:
            logger.debug("Telegram couldn't fetch animation URL directly, downloading: %s", url)

        data = await self._download_image(url)
        animation = BufferedInputFile(data, filename="animation.gif")
        await self._bot.send_animation(chat_id=chat_id, animation=animation, caption=caption or None)

    async def send_streaming(
        self, user_id: str, chunks: AsyncIterator[str]
    ) -> None:
        """Send a streaming response, editing the message as chunks arrive.

        When the response exceeds Telegram's 4096-char limit, the original
        message keeps the first portion and additional messages are sent for
        the remainder.
        """
        assert self._bot is not None
        chat_id = int(user_id)

        msg = await self._bot.send_message(chat_id=chat_id, text="...", parse_mode=None)
        buffer = ""
        last_edit = time.monotonic()
        edit_interval = 1.5
        safe_limit = MAX_TELEGRAM_MESSAGE_LENGTH - 200

        async for chunk in chunks:
            buffer += chunk
            now = time.monotonic()
            if now - last_edit >= edit_interval and len(buffer) > 3:
                display = buffer[:safe_limit] if len(buffer) > safe_limit else buffer
                with contextlib.suppress(Exception):
                    await msg.edit_text(display, parse_mode=None)
                last_edit = now

        if not buffer:
            return

        parts = split_message(buffer)
        # First part: edit the original streaming message
        try:
            await msg.edit_text(parts[0])
        except Exception:
            with contextlib.suppress(Exception):
                await msg.edit_text(parts[0], parse_mode=None)

        # Remaining parts: send as new messages so nothing is lost
        for part in parts[1:]:
            try:
                await self._bot.send_message(chat_id=chat_id, text=part)
            except Exception:
                with contextlib.suppress(Exception):
                    await self._bot.send_message(chat_id=chat_id, text=part, parse_mode=None)

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True  # Empty = allow all
        return user_id in self._allowed_users

    async def _resolve_identity(self, tg_user_id: int, full_name: str = "") -> str:
        """Resolve Telegram user ID to pincer_user_id. Falls back to channel-scoped ID."""
        if self._identity:
            try:
                return await self._identity.resolve(
                    ChannelType.TELEGRAM, tg_user_id, display_name=full_name or None,
                )
            except Exception:
                logger.debug("Identity resolution failed for %s", tg_user_id, exc_info=True)
        return ""

    def _register_handlers(self, router: Router) -> None:
        """Register all message handlers on the router."""

        @router.callback_query(F.data.startswith("tool_approve:") | F.data.startswith("tool_deny:"))
        async def handle_tool_approval(callback: CallbackQuery) -> None:
            data = callback.data or ""
            action, _, uid = data.partition(":")
            future = self._pending_approvals.get(uid)
            approved = action == "tool_approve"

            if future and not future.done():
                future.set_result(approved)

            label = "Approved" if approved else "Denied"
            await callback.answer(label)
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.edit_text(
                        f"{callback.message.text}\n\n— *{label}*",
                    )

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

            pincer_uid = await self._resolve_identity(
                message.from_user.id, message.from_user.full_name,
            )
            incoming = IncomingMessage(
                user_id=str(message.from_user.id),
                channel="telegram",
                text=message.caption or "",
                voice_data=data.getvalue(),
                voice_mime=voice.mime_type or "audio/ogg",
                raw=message,
                pincer_user_id=pincer_uid,
                channel_type=ChannelType.TELEGRAM,
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

            pincer_uid = await self._resolve_identity(
                message.from_user.id, message.from_user.full_name,
            )
            incoming = IncomingMessage(
                user_id=str(message.from_user.id),
                channel="telegram",
                text=message.caption or "What's in this image?",
                images=[(data.getvalue(), "image/jpeg")],
                raw=message,
                pincer_user_id=pincer_uid,
                channel_type=ChannelType.TELEGRAM,
            )

            response = await self._handler(incoming)
            for chunk in split_message(response):
                await message.answer(chunk)

        @router.message(F.document)
        async def handle_document(message: Message) -> None:
            if not message.from_user or not self._is_allowed(message.from_user.id):
                return
            assert self._bot is not None and self._handler is not None

            doc = message.document
            if not doc:
                return

            await self._bot.send_chat_action(message.chat.id, ChatAction.TYPING)

            file = await self._bot.get_file(doc.file_id)
            assert file.file_path is not None
            data = io.BytesIO()
            await self._bot.download_file(file.file_path, data)
            raw_bytes = data.getvalue()

            filename = doc.file_name or "unknown"
            mime = doc.mime_type or "application/octet-stream"

            pincer_uid = await self._resolve_identity(
                message.from_user.id, message.from_user.full_name,
            )
            image_mimes = {"image/jpeg", "image/png", "image/gif", "image/webp"}
            if mime in image_mimes:
                incoming = IncomingMessage(
                    user_id=str(message.from_user.id),
                    channel="telegram",
                    text=message.caption or "What's in this image?",
                    images=[(raw_bytes, mime)],
                    raw=message,
                    pincer_user_id=pincer_uid,
                    channel_type=ChannelType.TELEGRAM,
                )
            else:
                incoming = IncomingMessage(
                    user_id=str(message.from_user.id),
                    channel="telegram",
                    text=message.caption or "",
                    files=[(raw_bytes, mime, filename)],
                    raw=message,
                    pincer_user_id=pincer_uid,
                    channel_type=ChannelType.TELEGRAM,
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

            pincer_uid = await self._resolve_identity(
                message.from_user.id, message.from_user.full_name,
            )
            incoming = IncomingMessage(
                user_id=user_id,
                channel="telegram",
                text=message.text,
                raw=message,
                pincer_user_id=pincer_uid,
                channel_type=ChannelType.TELEGRAM,
            )

            response = await self._handler(incoming)
            for chunk in split_message(response):
                await message.answer(chunk)
