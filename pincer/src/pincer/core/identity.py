"""
Cross-channel identity resolver.

Maps Telegram user IDs and WhatsApp phone numbers to a unified
pincer_user_id. Ensures the same memory, session, and conversation
context persists across all channels.

Config mapping (in .env):
    PINCER_IDENTITY_MAP=telegram:12345=whatsapp:491234567890
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from pincer.channels.base import ChannelType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class IdentityResolver:
    """Resolves channel-specific user IDs to a unified Pincer user ID."""

    def __init__(self, db_path: Path, identity_map_config: str = "") -> None:
        self._db_path = str(db_path)
        self._identity_map_config = identity_map_config

    def _get_db(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self._db_path)

    async def ensure_table(self) -> None:
        """Create identity_map table if it doesn't exist."""
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("""
                CREATE TABLE IF NOT EXISTS identity_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pincer_user_id TEXT NOT NULL UNIQUE,
                    telegram_user_id INTEGER,
                    whatsapp_phone TEXT,
                    display_name TEXT,
                    preferred_channel TEXT DEFAULT 'telegram',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_telegram
                ON identity_map(telegram_user_id) WHERE telegram_user_id IS NOT NULL
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_whatsapp
                ON identity_map(whatsapp_phone) WHERE whatsapp_phone IS NOT NULL
            """)
            await db.commit()

    async def resolve(
        self,
        channel: ChannelType,
        channel_user_id: str | int,
        display_name: str | None = None,
    ) -> str:
        """
        Resolve a channel-specific user ID to a canonical Pincer user ID.

        Lookup order:
        1. Check identity_map table for existing mapping
        2. Check PINCER_IDENTITY_MAP config for pre-configured cross-channel link
        3. Create new identity
        """
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            existing = await self._find_existing(db, channel, channel_user_id)
            if existing:
                return existing

            mapped = await self._check_config_mapping(db, channel, channel_user_id)
            if mapped:
                return mapped

            pincer_user_id = self._generate_user_id(channel, channel_user_id)
            await self._create_identity(
                db, pincer_user_id, channel, channel_user_id, display_name,
            )
            return pincer_user_id

    async def _find_existing(
        self, db: aiosqlite.Connection, channel: ChannelType, channel_user_id: str | int,
    ) -> str | None:
        if channel == ChannelType.TELEGRAM:
            cursor = await db.execute(
                "SELECT pincer_user_id FROM identity_map WHERE telegram_user_id = ?",
                (int(channel_user_id),),
            )
        elif channel == ChannelType.WHATSAPP:
            phone = str(channel_user_id).lstrip("+")
            cursor = await db.execute(
                "SELECT pincer_user_id FROM identity_map WHERE whatsapp_phone = ?",
                (phone,),
            )
        else:
            return None

        row = await cursor.fetchone()
        return row[0] if row else None

    async def _check_config_mapping(
        self, db: aiosqlite.Connection, channel: ChannelType, channel_user_id: str | int,
    ) -> str | None:
        """
        Check PINCER_IDENTITY_MAP for pre-configured cross-channel links.
        Format: "telegram:12345=whatsapp:491234567890,telegram:67890=whatsapp:491111111111"
        """
        if not self._identity_map_config:
            return None

        for mapping in self._identity_map_config.split(","):
            mapping = mapping.strip()
            if "=" not in mapping:
                continue

            parts = mapping.split("=")
            if len(parts) != 2:
                continue

            left_channel, left_id = parts[0].split(":", 1)
            right_channel, right_id = parts[1].split(":", 1)

            current_key = f"{channel.value}:{str(channel_user_id).lstrip('+')}"

            other_channel, other_id = None, None
            if f"{left_channel}:{left_id}" == current_key:
                other_channel, other_id = right_channel, right_id
            elif f"{right_channel}:{right_id}" == current_key:
                other_channel, other_id = left_channel, left_id

            if other_channel is None:
                continue

            other_user_id = await self._find_existing(
                db, ChannelType(other_channel), other_id,
            )
            if other_user_id:
                await self._link_channel(db, other_user_id, channel, channel_user_id)
                logger.info(
                    "Identity linked: %s %s:%s -> %s",
                    other_user_id, channel.value, channel_user_id,
                )
                return other_user_id

        return None

    async def _create_identity(
        self,
        db: aiosqlite.Connection,
        pincer_user_id: str,
        channel: ChannelType,
        channel_user_id: str | int,
        display_name: str | None = None,
    ) -> None:
        telegram_id = int(channel_user_id) if channel == ChannelType.TELEGRAM else None
        whatsapp_phone = (
            str(channel_user_id).lstrip("+") if channel == ChannelType.WHATSAPP else None
        )

        await db.execute(
            """INSERT INTO identity_map
               (pincer_user_id, telegram_user_id, whatsapp_phone, display_name, preferred_channel)
               VALUES (?, ?, ?, ?, ?)""",
            (pincer_user_id, telegram_id, whatsapp_phone, display_name, channel.value),
        )
        await db.commit()
        logger.info("Identity created: %s (%s)", pincer_user_id, channel.value)

    async def _link_channel(
        self,
        db: aiosqlite.Connection,
        pincer_user_id: str,
        channel: ChannelType,
        channel_user_id: str | int,
    ) -> None:
        if channel == ChannelType.TELEGRAM:
            col, val = "telegram_user_id", int(channel_user_id)
        elif channel == ChannelType.WHATSAPP:
            col, val = "whatsapp_phone", str(channel_user_id).lstrip("+")
        else:
            return

        await db.execute(
            f"UPDATE identity_map SET {col} = ?, updated_at = datetime('now') "  # noqa: S608
            "WHERE pincer_user_id = ?",
            (val, pincer_user_id),
        )
        await db.commit()

    async def seed_from_config(self) -> None:
        """Pre-create identity rows from PINCER_IDENTITY_MAP so the router
        can resolve users immediately on startup (before any inbound message).

        Format: "telegram:12345=whatsapp:491234567890,telegram:67890=whatsapp:491111111111"
        """
        if not self._identity_map_config:
            return

        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            for mapping in self._identity_map_config.split(","):
                mapping = mapping.strip()
                if "=" not in mapping:
                    continue

                parts = mapping.split("=")
                if len(parts) != 2:
                    continue

                try:
                    left_channel, left_id = parts[0].strip().split(":", 1)
                    right_channel, right_id = parts[1].strip().split(":", 1)
                except ValueError:
                    continue

                telegram_id: int | None = None
                whatsapp_phone: str | None = None

                for ch, cid in ((left_channel, left_id), (right_channel, right_id)):
                    if ch == "telegram":
                        try:
                            telegram_id = int(cid)
                        except ValueError:
                            pass
                    elif ch == "whatsapp":
                        whatsapp_phone = cid.lstrip("+")

                if telegram_id is None and whatsapp_phone is None:
                    continue

                pincer_user_id = self._generate_user_id(
                    ChannelType(left_channel), left_id,
                )

                cursor = await db.execute(
                    "SELECT pincer_user_id, telegram_user_id, whatsapp_phone "
                    "FROM identity_map WHERE pincer_user_id = ?",
                    (pincer_user_id,),
                )
                existing = await cursor.fetchone()

                if existing:
                    needs_update = False
                    if telegram_id and not existing["telegram_user_id"]:
                        await db.execute(
                            "UPDATE identity_map SET telegram_user_id = ?, "
                            "updated_at = datetime('now') WHERE pincer_user_id = ?",
                            (telegram_id, pincer_user_id),
                        )
                        needs_update = True
                    if whatsapp_phone and not existing["whatsapp_phone"]:
                        await db.execute(
                            "UPDATE identity_map SET whatsapp_phone = ?, "
                            "updated_at = datetime('now') WHERE pincer_user_id = ?",
                            (whatsapp_phone, pincer_user_id),
                        )
                        needs_update = True
                    if needs_update:
                        logger.info("Identity updated from config: %s", pincer_user_id)
                else:
                    await db.execute(
                        """INSERT INTO identity_map
                           (pincer_user_id, telegram_user_id, whatsapp_phone,
                            preferred_channel)
                           VALUES (?, ?, ?, ?)""",
                        (pincer_user_id, telegram_id, whatsapp_phone, left_channel),
                    )
                    logger.info(
                        "Identity seeded from config: %s (tg=%s, wa=%s)",
                        pincer_user_id, telegram_id, whatsapp_phone,
                    )
            await db.commit()

    async def get_preferred_channel(self, pincer_user_id: str) -> tuple[ChannelType, str]:
        """
        Get user's preferred channel for proactive messages.
        Returns (channel_type, chat_id).
        """
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_user_id, whatsapp_phone, preferred_channel "
                "FROM identity_map WHERE pincer_user_id = ?",
                (pincer_user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Unknown user: {pincer_user_id}")

            telegram_id, whatsapp_phone, preferred = row

            if preferred == "whatsapp" and whatsapp_phone:
                return ChannelType.WHATSAPP, whatsapp_phone
            if preferred == "telegram" and telegram_id:
                return ChannelType.TELEGRAM, str(telegram_id)

            if whatsapp_phone:
                return ChannelType.WHATSAPP, whatsapp_phone
            if telegram_id:
                return ChannelType.TELEGRAM, str(telegram_id)

            raise ValueError(f"No channels linked for user: {pincer_user_id}")

    async def get_all_channels(self, pincer_user_id: str) -> dict[ChannelType, str]:
        """Get all linked channels for a user."""
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_user_id, whatsapp_phone FROM identity_map "
                "WHERE pincer_user_id = ?",
                (pincer_user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return {}

            channels: dict[ChannelType, str] = {}
            telegram_id, whatsapp_phone = row
            if telegram_id:
                channels[ChannelType.TELEGRAM] = str(telegram_id)
            if whatsapp_phone:
                channels[ChannelType.WHATSAPP] = whatsapp_phone
            return channels

    @staticmethod
    def _generate_user_id(channel: ChannelType, channel_user_id: str | int) -> str:
        raw = f"{channel.value}:{channel_user_id}"
        return f"usr_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
