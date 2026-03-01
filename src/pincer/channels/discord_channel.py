"""
Discord channel implementation using discord.py.

Features:
- DM conversations
- Slash commands (/ask, /search, /run, /status)
- Thread-based conversations with separate sessions
- @mention handling in guild channels
- Rich embeds for structured responses
- Attachment processing (text, images, other)
- Proactive messaging support
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE_LENGTH = 2000


class ResponseStyle(StrEnum):
    PLAIN = "plain"
    CODE = "code"
    EMBED = "embed"


def split_message(text: str, limit: int = MAX_DISCORD_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks respecting Discord's character limit."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try splitting at newline
        split_at = remaining.rfind("\n", 0, limit)
        if split_at > 0:
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at + 1:]
            continue

        # Try splitting at space
        split_at = remaining.rfind(" ", 0, limit)
        if split_at > 0:
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at + 1:]
            continue

        # Hard cut
        chunks.append(remaining[:limit])
        remaining = remaining[limit:]

    return chunks if chunks else [""]


def detect_response_style(text: str) -> ResponseStyle:
    """Detect appropriate Discord rendering style for agent output."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return ResponseStyle.CODE
    if len(text) > 800 and ("## " in text or "### " in text or "\n- " in text):
        return ResponseStyle.EMBED
    return ResponseStyle.PLAIN


def make_embed(
    title: str = "",
    description: str = "",
    color: int = 0x5865F2,
    fields: list[dict[str, Any]] | None = None,
    footer: str = "",
) -> dict[str, Any]:
    """Build a Discord embed dict (converted to discord.Embed at send time)."""
    embed: dict[str, Any] = {"color": color}
    if title:
        embed["title"] = title
    if description:
        embed["description"] = description[:4096]
    if fields:
        embed["fields"] = []
        for f in fields[:25]:
            embed["fields"].append({
                "name": f.get("name", "")[:256],
                "value": f.get("value", "")[:1024],
                "inline": f.get("inline", False),
            })
    if footer:
        embed["footer"] = {"text": footer}
    return embed


def text_to_embed(text: str) -> dict[str, Any]:
    """Parse markdown-structured text into a Discord embed dict."""
    lines = text.strip().splitlines()
    if not lines:
        return make_embed(description=text)

    title = ""
    description_lines: list[str] = []
    fields: list[dict[str, Any]] = []
    current_field_name = ""
    current_field_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            # Flush previous field
            if current_field_name:
                fields.append({
                    "name": current_field_name,
                    "value": "\n".join(current_field_lines).strip() or "\u200b",
                })
                current_field_lines = []
            heading = stripped.lstrip("#").strip()
            if not title and not fields and not description_lines:
                title = heading
            else:
                current_field_name = heading
        elif current_field_name:
            current_field_lines.append(line)
        elif not title and not fields:
            # First non-heading line before any section
            if stripped:
                if not title:
                    title = stripped
                else:
                    description_lines.append(line)
            else:
                description_lines.append(line)
        else:
            description_lines.append(line)

    if current_field_name:
        fields.append({
            "name": current_field_name,
            "value": "\n".join(current_field_lines).strip() or "\u200b",
        })

    return make_embed(
        title=title,
        description="\n".join(description_lines).strip(),
        fields=fields if fields else None,
    )


def _process_attachments(attachments: list[Any]) -> list[str]:
    """Process Discord attachments into text descriptions."""
    parts: list[str] = []
    for att in attachments:
        content_type = getattr(att, "content_type", "") or ""
        name = getattr(att, "filename", "unknown")
        if content_type.startswith("text/"):
            try:
                data = asyncio.get_event_loop().run_until_complete(att.read())
                text = data.decode("utf-8", errors="replace")[:4000]
                parts.append(f"[File: {name}]\n{text}")
            except Exception:
                parts.append(f"[File: {name}]")
        elif content_type.startswith("image/"):
            w = getattr(att, "width", "?")
            h = getattr(att, "height", "?")
            url = getattr(att, "url", "")
            parts.append(f"[Image: {name}, {w}x{h}, url={url}]")
        else:
            size = getattr(att, "size", 0)
            parts.append(f"[Attachment: {name}, type={content_type}, size={size}]")
    return parts


async def _process_attachments_async(attachments: list[Any]) -> list[str]:
    """Process Discord attachments into text descriptions (async)."""
    parts: list[str] = []
    for att in attachments:
        content_type = getattr(att, "content_type", "") or ""
        name = getattr(att, "filename", "unknown")
        if content_type.startswith("text/"):
            try:
                data = await att.read()
                text = data.decode("utf-8", errors="replace")[:4000]
                parts.append(f"[File: {name}]\n{text}")
            except Exception:
                parts.append(f"[File: {name}]")
        elif content_type.startswith("image/"):
            w = getattr(att, "width", "?")
            h = getattr(att, "height", "?")
            url = getattr(att, "url", "")
            parts.append(f"[Image: {name}, {w}x{h}, url={url}]")
        else:
            size = getattr(att, "size", 0)
            parts.append(f"[Attachment: {name}, type={content_type}, size={size}]")
    return parts


class DiscordChannel(BaseChannel):
    """Discord bot channel with DM, slash commands, and thread support."""

    channel_type = ChannelType.DISCORD

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._identity: IdentityResolver | None = None
        self._bot: Any = None  # commands.Bot
        self._tree: Any = None  # app_commands.CommandTree
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._agent: Any = None

    def set_identity_resolver(self, identity: IdentityResolver) -> None:
        """Set the identity resolver for cross-channel user mapping."""
        self._identity = identity

    def set_agent(self, agent: Any) -> None:
        """Set the agent for /status command access."""
        self._agent = agent

    @property
    def name(self) -> str:
        return "discord"

    async def start(self, handler: MessageHandler) -> None:
        """Start the Discord bot. No-op if token is not configured."""
        token = self._settings.discord_bot_token.get_secret_value()
        if not token:
            logger.warning("Discord bot token not set, channel disabled")
            return

        self._handler = handler

        try:
            import discord
            from discord import app_commands
            from discord.ext import commands
        except ImportError:
            logger.error("discord.py not installed, run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guilds = True
        intents.members = False

        bot = commands.Bot(command_prefix="!", intents=intents)
        self._bot = bot
        self._tree = bot.tree
        channel = self  # capture for closures

        @bot.event
        async def on_ready() -> None:
            try:
                synced = await bot.tree.sync()
                logger.info("Synced %d slash commands", len(synced))
            except discord.HTTPException as e:
                logger.error("Failed to sync slash commands: %s", e)
            channel._ready.set()
            logger.info("Discord bot connected as %s", bot.user)

        # -- Slash commands --

        @bot.tree.command(name="ask", description="Ask the AI a question")
        @app_commands.describe(question="Your question")
        async def cmd_ask(interaction: discord.Interaction, question: str) -> None:
            await interaction.response.defer(thinking=True)
            try:
                thread_name = f"\U0001f916 {question[:90]}"
                thread = await interaction.channel.create_thread(
                    name=thread_name,
                    auto_archive_duration=1440,
                )
                response = await channel._dispatch(
                    user_id=str(interaction.user.id),
                    display_name=interaction.user.display_name,
                    text=question,
                    session_channel=f"discord-thread-{thread.id}",
                )
                await thread.send(response)
                await interaction.followup.send(f"Continuing in {thread.mention}")
            except Exception:
                logger.exception("Error in /ask command")
                await interaction.followup.send("\u26a0\ufe0f Something went wrong.")

        @bot.tree.command(name="search", description="Search the web")
        @app_commands.describe(query="Search query")
        async def cmd_search(interaction: discord.Interaction, query: str) -> None:
            await interaction.response.defer(thinking=True)
            try:
                response = await channel._dispatch(
                    user_id=str(interaction.user.id),
                    display_name=interaction.user.display_name,
                    text=f"Search the web for: {query}",
                    session_channel="discord",
                )
                for chunk in split_message(response):
                    await interaction.followup.send(chunk)
            except Exception:
                logger.exception("Error in /search command")
                await interaction.followup.send("\u26a0\ufe0f Something went wrong.")

        @bot.tree.command(name="run", description="Execute a command through the agent")
        @app_commands.describe(command="The command to execute")
        async def cmd_run(interaction: discord.Interaction, command: str) -> None:
            await interaction.response.defer(thinking=True)
            try:
                response = await channel._dispatch(
                    user_id=str(interaction.user.id),
                    display_name=interaction.user.display_name,
                    text=f"Execute this: {command}",
                    session_channel="discord",
                )
                for chunk in split_message(response):
                    await interaction.followup.send(chunk)
            except Exception:
                logger.exception("Error in /run command")
                await interaction.followup.send("\u26a0\ufe0f Something went wrong.")

        @bot.tree.command(name="status", description="Show bot status")
        async def cmd_status(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            try:
                embed = discord.Embed(
                    title=f"{channel._settings.agent_name} Status",
                    color=0x57F287,
                )
                active_types = ("telegram", "whatsapp", "discord")
                active = [ct.value for ct in ChannelType if ct.value in active_types]
                embed.add_field(name="Channels", value=", ".join(active), inline=True)

                tool_count = "N/A"
                today_cost = "N/A"
                model_name = channel._settings.default_model
                if channel._agent:
                    import contextlib
                    with contextlib.suppress(Exception):
                        tool_count = str(len(channel._agent._tools.list_tools()))
                    with contextlib.suppress(Exception):
                        cost = await channel._agent._costs.get_today_spend()
                        today_cost = f"${cost:.4f}"

                embed.add_field(name="Tools", value=str(tool_count), inline=True)
                embed.add_field(name="Today's Cost", value=today_cost, inline=True)
                embed.add_field(name="Model", value=model_name, inline=True)
                embed.add_field(name="Version", value="0.4.0", inline=True)
                await interaction.followup.send(embed=embed)
            except Exception:
                logger.exception("Error in /status command")
                await interaction.followup.send("\u26a0\ufe0f Something went wrong.")

        # -- Message handler --

        @bot.event
        async def on_message(message: discord.Message) -> None:
            if message.author == bot.user:
                return
            if message.author.bot:
                return

            # DM handling
            if isinstance(message.channel, discord.DMChannel):
                await channel._handle_dm(message)
                return

            # Thread handling
            if isinstance(message.channel, discord.Thread):
                await channel._handle_thread(message)
                return

            # Guild channel: only respond to @mentions
            if bot.user and bot.user.mentioned_in(message):
                await channel._handle_mention(message)

        # Start bot in background
        async def _run_bot() -> None:
            try:
                await bot.start(token)
            except discord.LoginFailure:
                logger.error("Discord login failed — check PINCER_DISCORD_BOT_TOKEN")
            except Exception:
                logger.exception("Discord bot crashed")

        self._task = asyncio.create_task(_run_bot())

        # Wait for ready with timeout
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
        except TimeoutError:
            logger.warning("Discord bot did not become ready within 30s, continuing")

    async def stop(self) -> None:
        """Disconnect the Discord bot."""
        if self._bot:
            await self._bot.close()
        if self._task and not self._task.done():
            self._task.cancel()
            import contextlib
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Discord channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a proactive message to a user."""
        if not self._bot:
            logger.warning("Discord bot not connected, cannot send")
            return

        import discord

        thread_id = kwargs.get("thread_id")
        channel_id = kwargs.get("channel_id")

        try:
            target = None
            if thread_id:
                target = self._bot.get_channel(int(thread_id))
            elif channel_id:
                target = self._bot.get_channel(int(channel_id))
            else:
                user = await self._bot.fetch_user(int(user_id))
                target = await user.create_dm()

            if target:
                for chunk in split_message(text):
                    await target.send(chunk)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.error("Failed to send proactive message to %s: %s", user_id, e)

    # -- Internal dispatch --

    async def _dispatch(
        self,
        user_id: str,
        display_name: str,
        text: str,
        session_channel: str,
    ) -> str:
        """Dispatch a message through the identity resolver and handler."""
        if not self._handler:
            return "\u26a0\ufe0f Agent not ready."

        pincer_user_id = ""
        if self._identity:
            pincer_user_id = await self._identity.resolve(
                channel=ChannelType.DISCORD,
                channel_user_id=user_id,
                display_name=display_name,
            )

        incoming = IncomingMessage(
            user_id=pincer_user_id or user_id,
            channel=session_channel,
            text=text,
            channel_type=ChannelType.DISCORD,
            pincer_user_id=pincer_user_id,
        )

        try:
            return await self._handler(incoming)
        except Exception:
            logger.exception("Agent error for Discord user %s", user_id)
            return "\u26a0\ufe0f Something went wrong. Please try again."

    async def _handle_dm(self, message: Any) -> None:
        """Handle a DM message."""
        text = message.content
        if message.attachments:
            att_texts = await _process_attachments_async(message.attachments)
            if att_texts:
                text = text + "\n\n[Attachments]\n" + "\n".join(att_texts)

        async with message.channel.typing():
            response = await self._dispatch(
                user_id=str(message.author.id),
                display_name=message.author.display_name,
                text=text,
                session_channel="discord",
            )

        await self._send_response(message.channel, response)

    async def _handle_thread(self, message: Any) -> None:
        """Handle a message in a thread."""
        thread = message.channel
        is_ours = (
            (hasattr(thread, "owner_id") and self._bot and thread.owner_id == self._bot.user.id)
            or (hasattr(thread, "name") and thread.name.startswith("\U0001f916 "))
        )
        is_mentioned = self._bot and self._bot.user and self._bot.user.mentioned_in(message)

        if not is_ours and not is_mentioned:
            return

        text = message.content
        if self._bot and self._bot.user:
            text = text.replace(f"<@{self._bot.user.id}>", "").strip()

        if message.attachments:
            att_texts = await _process_attachments_async(message.attachments)
            if att_texts:
                text = text + "\n\n[Attachments]\n" + "\n".join(att_texts)

        async with thread.typing():
            response = await self._dispatch(
                user_id=str(message.author.id),
                display_name=message.author.display_name,
                text=text,
                session_channel=f"discord-thread-{thread.id}",
            )

        await self._send_response(thread, response)

    async def _handle_mention(self, message: Any) -> None:
        """Handle an @mention in a guild channel by creating a thread."""
        import discord

        text = message.content
        if self._bot and self._bot.user:
            text = text.replace(f"<@{self._bot.user.id}>", "").strip()

        if not text:
            return

        try:
            thread_name = f"\U0001f916 {text[:90]}"
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
            )
        except discord.HTTPException:
            logger.warning("Failed to create thread, replying inline")
            async with message.channel.typing():
                response = await self._dispatch(
                    user_id=str(message.author.id),
                    display_name=message.author.display_name,
                    text=text,
                    session_channel="discord",
                )
            await self._send_response(message.channel, response)
            return

        async with thread.typing():
            response = await self._dispatch(
                user_id=str(message.author.id),
                display_name=message.author.display_name,
                text=text,
                session_channel=f"discord-thread-{thread.id}",
            )

        await self._send_response(thread, response)

    async def _send_response(self, channel: Any, text: str) -> None:
        """Send a response with appropriate formatting."""
        import discord

        style = detect_response_style(text)

        if style == ResponseStyle.EMBED:
            embed_data = text_to_embed(text)
            embed = discord.Embed(
                title=embed_data.get("title", ""),
                description=embed_data.get("description", ""),
                color=embed_data.get("color", 0x5865F2),
            )
            for f in embed_data.get("fields", []):
                embed.add_field(
                    name=f["name"], value=f["value"], inline=f.get("inline", False)
                )
            footer = embed_data.get("footer")
            if footer:
                embed.set_footer(text=footer["text"])
            await channel.send(embed=embed)
        else:
            for chunk in split_message(text):
                await channel.send(chunk)

    def _is_our_thread(self, thread: Any) -> bool:
        """Check if a thread was created by this bot."""
        if (
            hasattr(thread, "owner_id")
            and self._bot
            and self._bot.user
            and thread.owner_id == self._bot.user.id
        ):
            return True
        return hasattr(thread, "name") and thread.name.startswith("\U0001f916 ")
