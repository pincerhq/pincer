"""
Cross-channel identity resolver.

Maps channel-specific IDs (telegram:12345, whatsapp:491234567890, etc.) to a
unified pincer_user_id. Ensures the same memory and context persists across
all channels.

Schema
------
identity_meta     — one row per pincer user (preferred channel, display name)
channel_identities — many-to-many: (channel, channel_user_id) → pincer_user_id

Config mapping (.env)
---------------------
Named canonical ID (recommended — human-readable, stable across environments):
    PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890

Auto-generated hash ID (backward-compatible, opaque):
    PINCER_IDENTITY_MAP=telegram:johnDoe=whatsapp:491234567890

Multiple channels per identity and multiple identities separated by commas:
    PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890=signal:491234567890,jane@telegram:janeAustin=whatsapp:491111111111

The optional name prefix sets the pincer_user_id directly (e.g. "user:john" in
memory tags) instead of the auto-generated "user:usr_abc123..." hash.
Memory stored under a named ID is portable across DB rebuilds as long as the
config entry stays the same.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

import aiosqlite

from pincer.channels.base import ChannelType

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.channels.base import BaseChannel

logger = logging.getLogger(__name__)

# Channels whose IDs are phone numbers — leading "+" is stripped for storage
_PHONE_CHANNELS = {ChannelType.WHATSAPP, ChannelType.VOICE, ChannelType.SIGNAL}


class IdentityResolver:
    """Resolves channel-specific user IDs to a unified Pincer user ID."""

    def __init__(self, db_path: Path, identity_map_config: str = "") -> None:
        self._db_path = str(db_path)
        self._identity_map_config = identity_map_config

    @property
    def has_config(self) -> bool:
        return bool(self._identity_map_config)

    def _get_db(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self._db_path)

    @staticmethod
    def _normalize_id(channel: ChannelType | str, channel_user_id: str | int) -> str:
        """Return a canonical string form of a channel user ID."""
        ch = ChannelType(channel) if isinstance(channel, str) else channel
        s = str(channel_user_id)
        return s.lstrip("+") if ch in _PHONE_CHANNELS else s

    async def ensure_table(self) -> None:
        """Create schema tables and migrate from legacy identity_map if present."""
        async with self._get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS identity_meta (
                    pincer_user_id TEXT PRIMARY KEY,
                    preferred_channel TEXT,
                    display_name TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS channel_identities (
                    channel TEXT NOT NULL,
                    channel_user_id TEXT NOT NULL,
                    pincer_user_id TEXT NOT NULL
                        REFERENCES identity_meta(pincer_user_id),
                    created_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (channel, channel_user_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_ci_pincer
                ON channel_identities(pincer_user_id)
            """)
            await db.commit()
            await self._migrate_legacy(db)

    async def _migrate_legacy(self, db: aiosqlite.Connection) -> None:
        """Migrate old identity_map rows into the new schema (no-op if absent)."""
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='identity_map'")
        if not await cursor.fetchone():
            return

        cursor = await db.execute("PRAGMA table_info(identity_map)")
        col_names = {row[1] for row in await cursor.fetchall()}

        def _sel(col: str, default: str = "NULL") -> str:
            return col if col in col_names else default

        query = (
            f"SELECT pincer_user_id, telegram_user_id, whatsapp_phone, discord_user_id, "
            f"{_sel('phone_number')}, {_sel('signal_phone')}, {_sel('slack_user_id')}, "
            f"{_sel('display_name')}, {_sel('preferred_channel', repr('telegram'))} "
            f"FROM identity_map"
        )
        cursor = await db.execute(query)
        rows = await cursor.fetchall()
        if not rows:
            return

        migrated = 0
        for row in rows:
            pincer_uid, tg, wa, dc, ph, sig, slk, dname, preferred = row
            await db.execute(
                "INSERT OR IGNORE INTO identity_meta "
                "(pincer_user_id, preferred_channel, display_name) VALUES (?, ?, ?)",
                (pincer_uid, preferred, dname),
            )
            for channel_name, val in (
                ("telegram", str(tg) if tg is not None else None),
                ("whatsapp", wa),
                ("discord", dc),
                ("voice", ph),
                ("signal", sig),
                ("slack", slk),
            ):
                if val:
                    await db.execute(
                        "INSERT OR IGNORE INTO channel_identities "
                        "(channel, channel_user_id, pincer_user_id) VALUES (?, ?, ?)",
                        (channel_name, val, pincer_uid),
                    )
            migrated += 1

        await db.commit()
        logger.info("Migrated %d legacy identity rows to new schema", migrated)

    async def find(
        self,
        channel: ChannelType,
        channel_user_id: str | int,
    ) -> str | None:
        """Look up an existing identity without creating one.

        Returns the pincer_user_id if found via DB or config mapping, else None.
        Unlike resolve(), this never writes to the database.
        """
        normalized = self._normalize_id(channel, channel_user_id)
        async with self._get_db() as db:
            existing = await self._find_existing(db, channel, normalized)
            if existing:
                return existing
            return await self._check_config_mapping(db, channel, normalized)

    async def resolve(
        self,
        channel: ChannelType,
        channel_user_id: str | int,
        display_name: str | None = None,
    ) -> str:
        """
        Resolve a channel-specific user ID to a canonical Pincer user ID.

        Lookup order:
        1. Existing mapping in channel_identities
        2. Pre-configured cross-channel link from PINCER_IDENTITY_MAP
        3. Create new identity
        """
        normalized = self._normalize_id(channel, channel_user_id)
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            existing = await self._find_existing(db, channel, normalized)
            if existing:
                return existing

            mapped = await self._check_config_mapping(db, channel, normalized)
            if mapped:
                return mapped

            pincer_user_id = self._generate_user_id(channel, normalized)
            await self._create_identity(db, pincer_user_id, channel, normalized, display_name)
            return pincer_user_id

    async def link_if_new(
        self,
        pincer_user_id: str,
        channel: ChannelType,
        channel_user_id: str | int,
    ) -> None:
        """Link channel_user_id to an existing pincer_user_id if not already linked."""
        normalized = self._normalize_id(channel, channel_user_id)
        async with self._get_db() as db:
            existing = await self._find_existing(db, channel, normalized)
            if not existing:
                await self._link_channel(db, pincer_user_id, channel, normalized)

    async def _find_existing(
        self,
        db: aiosqlite.Connection,
        channel: ChannelType,
        normalized_id: str,
    ) -> str | None:
        cursor = await db.execute(
            "SELECT pincer_user_id FROM channel_identities WHERE channel = ? AND channel_user_id = ?",
            (channel.value, normalized_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def _create_identity(
        self,
        db: aiosqlite.Connection,
        pincer_user_id: str,
        channel: ChannelType,
        normalized_id: str,
        display_name: str | None = None,
    ) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO identity_meta (pincer_user_id, preferred_channel, display_name) VALUES (?, ?, ?)",
            (pincer_user_id, channel.value, display_name),
        )
        await db.execute(
            "INSERT OR IGNORE INTO channel_identities (channel, channel_user_id, pincer_user_id) VALUES (?, ?, ?)",
            (channel.value, normalized_id, pincer_user_id),
        )
        await db.commit()
        logger.info("Identity created: %s (%s:%s)", pincer_user_id, channel.value, normalized_id)

    async def _link_channel(
        self,
        db: aiosqlite.Connection,
        pincer_user_id: str,
        channel: ChannelType,
        normalized_id: str,
    ) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO channel_identities (channel, channel_user_id, pincer_user_id) VALUES (?, ?, ?)",
            (channel.value, normalized_id, pincer_user_id),
        )
        await db.commit()
        logger.info("Identity linked: %s ← %s:%s", pincer_user_id, channel.value, normalized_id)

    async def _rename_identity(
        self,
        db: aiosqlite.Connection,
        old_id: str,
        new_id: str,
    ) -> None:
        """Rename a pincer_user_id across identity_meta, channel_identities, and sessions.

        Only called for auto-generated hash IDs (usr_...) to avoid overwriting
        intentionally-named identities.
        """
        await db.execute(
            "INSERT OR IGNORE INTO identity_meta "
            "(pincer_user_id, preferred_channel, display_name) "
            "SELECT ?, preferred_channel, display_name FROM identity_meta "
            "WHERE pincer_user_id = ?",
            (new_id, old_id),
        )
        await db.execute(
            "UPDATE channel_identities SET pincer_user_id = ? WHERE pincer_user_id = ?",
            (new_id, old_id),
        )
        await db.execute("DELETE FROM identity_meta WHERE pincer_user_id = ?", (old_id,))
        try:
            await db.execute(
                "UPDATE sessions SET user_id = ? WHERE user_id = ?",
                (new_id, old_id),
            )
            await db.execute(
                "UPDATE sessions SET session_id = replace(session_id, ?, ?) WHERE session_id LIKE ?",
                (old_id, new_id, f"%{old_id}%"),
            )
        except Exception:
            pass  # sessions table may not exist yet
        logger.info(
            "Identity renamed: %s → %s "
            "(memory tags in MCP server still use %s — run 'pincer migrate-memories' to update)",
            old_id,
            new_id,
            old_id,
        )

    async def _check_config_mapping(
        self,
        db: aiosqlite.Connection,
        channel: ChannelType,
        normalized_id: str,
    ) -> str | None:
        """Check PINCER_IDENTITY_MAP for a pre-configured cross-channel link."""
        if not self._identity_map_config:
            return None

        current_key = f"{channel.value}:{normalized_id}"

        for raw_entry in self._identity_map_config.split(","):
            if "=" not in raw_entry:
                continue
            try:
                name, pairs = self._parse_mapping(raw_entry)
            except ValueError:
                continue

            norm_pairs = [(ch, self._normalize_id(ChannelType(ch), cid)) for ch, cid in pairs]

            # Check if the incoming channel:id is in this entry
            entry_keys = {f"{ch}:{cid}" for ch, cid in norm_pairs}
            if current_key not in entry_keys:
                continue

            # Look for an existing identity among the other channels in this entry
            for ch, cid in norm_pairs:
                if f"{ch}:{cid}" == current_key:
                    continue
                uid = await self._find_existing(db, ChannelType(ch), cid)
                if uid:
                    await self._link_channel(db, uid, channel, normalized_id)
                    return uid

            # No other side exists yet — establish a named identity now if configured
            if name:
                await self._create_identity(db, name, channel, normalized_id)
                return name

        return None

    async def _resolve_via_channel(
        self,
        ch_type: ChannelType,
        normalized_id: str,
        channels: dict[ChannelType, BaseChannel],
    ) -> str:
        """Ask a channel to translate a normalized config ID to its internal ID."""
        channel = channels.get(ch_type)
        if channel is None:
            return normalized_id
        try:
            resolved = await channel.resolve_internal_user_id(normalized_id)
            logger.info("Channel %s: resolve_internal_user_id success %r -> %r", ch_type.value, normalized_id, resolved)
            return self._normalize_id(ch_type, resolved)
        except Exception as e:
            logger.error(
                "Channel %s: resolve_internal_user_id failed for %r: %s",
                ch_type.value,
                normalized_id,
                e,
            )
            return normalized_id

    async def cleanup(
        self,
        channels: dict[ChannelType, BaseChannel] | None = None,
    ) -> None:
        """Remove stale rows from the identity tables.

        If PINCER_IDENTITY_MAP is configured, the resolved internal IDs for
        every channel pair are the authoritative whitelist. Any
        channel_identities row whose (channel, channel_user_id) is NOT in the
        whitelist is deleted, and any identity_meta row left with no channels is
        deleted with it. Passing ``channels`` allows config IDs to be translated
        to real internal IDs before the whitelist is built.

        When no config is set the method is a no-op (nothing to enforce).
        """
        if not self._identity_map_config:
            return

        allowed: set[tuple[str, str]] = set()
        for raw_entry in self._identity_map_config.split(","):
            if "=" not in raw_entry:
                continue
            try:
                _, pairs = self._parse_mapping(raw_entry)
            except ValueError:
                continue
            for ch, cid in pairs:
                ch_type = ChannelType(ch)
                norm = self._normalize_id(ch_type, cid)
                if channels:
                    norm = await self._resolve_via_channel(ch_type, norm, channels)
                allowed.add((ch, norm))

        async with self._get_db() as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='identity_meta'")
            if not await cursor.fetchone():
                return

            cursor = await db.execute("SELECT channel, channel_user_id FROM channel_identities")
            rows = await cursor.fetchall()

            to_delete = [(ch, cid) for ch, cid in rows if (ch, cid) not in allowed]
            for ch, cid in to_delete:
                await db.execute(
                    "DELETE FROM channel_identities WHERE channel = ? AND channel_user_id = ?",
                    (ch, cid),
                )

            cursor = await db.execute(
                """
                DELETE FROM identity_meta
                WHERE pincer_user_id NOT IN (SELECT pincer_user_id FROM channel_identities)
                """
            )
            channelless = cursor.rowcount

            await db.commit()

        if to_delete or channelless:
            logger.info(
                "Identity cleanup: removed %d unlisted channel link(s), %d channelless identity/identities",
                len(to_delete),
                channelless,
            )

    async def seed_from_config(
        self,
        channels: dict[ChannelType, BaseChannel] | None = None,
    ) -> None:
        """Pre-create identity rows from PINCER_IDENTITY_MAP on startup.

        Each config entry may contain N channel:id pairs separated by '='.
        All pairs in one entry share a single pincer_user_id. Passing
        ``channels`` causes each config ID to be translated to the channel's
        real internal ID (e.g. WhatsApp phone → LID, Telegram @username →
        numeric user_id) before being stored.
        """
        if not self._identity_map_config:
            return

        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            for raw_entry in self._identity_map_config.split(","):
                if "=" not in raw_entry:
                    continue
                try:
                    name, pairs = self._parse_mapping(raw_entry)
                except ValueError:
                    logger.warning("Invalid identity map entry: %r", raw_entry.strip())
                    continue

                # Resolve each pair's ID through the channel (phone → LID, etc.)
                resolved: list[tuple[str, ChannelType, str]] = []
                for ch_str, cid in pairs:
                    try:
                        ch_type = ChannelType(ch_str)
                    except ValueError:
                        logger.warning("Unknown channel %r in identity map entry %r", ch_str, raw_entry.strip())
                        continue
                    norm = self._normalize_id(ch_type, cid)
                    if channels:
                        norm = await self._resolve_via_channel(ch_type, norm, channels)
                    resolved.append((ch_str, ch_type, norm))

                if not resolved:
                    continue

                # Collect all existing identities for the channels in this entry
                existing_uids: list[str] = []
                for _ch_str, ch_type, norm in resolved:
                    uid = await self._find_existing(db, ch_type, norm)
                    if uid and uid not in existing_uids:
                        existing_uids.append(uid)

                # Determine or create the canonical pincer_user_id
                if len(existing_uids) > 1:
                    # Multiple distinct identities — merge them all into one
                    target_uid = name or existing_uids[0]
                    for uid in existing_uids:
                        if uid != target_uid:
                            logger.info("Identity conflict resolved: merging %s into %s", uid, target_uid)
                            await self._rename_identity(db, uid, target_uid)
                    pincer_uid = target_uid
                elif len(existing_uids) == 1:
                    existing_uid = existing_uids[0]
                    if name:
                        if existing_uid == name:
                            pincer_uid = name
                        elif existing_uid.startswith("usr_"):
                            await self._rename_identity(db, existing_uid, name)
                            pincer_uid = name
                        else:
                            logger.warning(
                                "Named canonical ID %r ignored: identity already has name %r",
                                name,
                                existing_uid,
                            )
                            pincer_uid = existing_uid
                    else:
                        pincer_uid = existing_uid
                else:
                    # No existing identity — create fresh
                    if name:
                        pincer_uid = name
                    else:
                        first_ch_str, first_ch_type, first_norm = resolved[0]
                        pincer_uid = self._generate_user_id(first_ch_type, first_norm)

                first_channel = resolved[0][0]
                await db.execute(
                    "INSERT OR IGNORE INTO identity_meta (pincer_user_id, preferred_channel) VALUES (?, ?)",
                    (pincer_uid, first_channel),
                )
                for ch_str, _ch_type, norm in resolved:
                    await db.execute(
                        "INSERT OR IGNORE INTO channel_identities "
                        "(channel, channel_user_id, pincer_user_id) VALUES (?, ?, ?)",
                        (ch_str, norm, pincer_uid),
                    )

            await db.commit()
            logger.info("Identity config seeded")

    async def get_preferred_channel(self, pincer_user_id: str) -> tuple[ChannelType, str]:
        """Get user's preferred channel for proactive messages."""
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT preferred_channel FROM identity_meta WHERE pincer_user_id = ?",
                (pincer_user_id,),
            )
            meta = await cursor.fetchone()
            if not meta:
                raise ValueError(f"Unknown user: {pincer_user_id}")

            preferred = meta["preferred_channel"]
            all_channels = await self._get_all_channels_raw(db, pincer_user_id)

            if preferred and preferred in all_channels:
                return ChannelType(preferred), all_channels[preferred]

            for ch_name, ch_id in all_channels.items():
                return ChannelType(ch_name), ch_id

            raise ValueError(f"No channels linked for user: {pincer_user_id}")

    async def get_all_channels(self, pincer_user_id: str) -> dict[ChannelType, str]:
        """Get all linked channels for a user (one entry per channel type)."""
        async with self._get_db() as db:
            raw = await self._get_all_channels_raw(db, pincer_user_id)
        return {ChannelType(ch): cid for ch, cid in raw.items()}

    async def _get_all_channels_raw(
        self,
        db: aiosqlite.Connection,
        pincer_user_id: str,
    ) -> dict[str, str]:
        """Return first seen channel_user_id per channel for this user."""
        cursor = await db.execute(
            "SELECT channel, channel_user_id FROM channel_identities WHERE pincer_user_id = ? ORDER BY created_at",
            (pincer_user_id,),
        )
        result: dict[str, str] = {}
        async for row in cursor:
            if row[0] not in result:
                result[row[0]] = row[1]
        return result

    @staticmethod
    def _parse_mapping(
        entry: str,
    ) -> tuple[str | None, list[tuple[str, str]]]:
        """Parse one config entry into (name, [(channel, id), ...]).

        Formats accepted:
            name@ch1:id1=ch2:id2=ch3:id3   (named canonical ID, N channels)
            ch1:id1=ch2:id2                 (hash-based, backward compat)

        Raises ValueError if fewer than 2 channel:id pairs are found.
        """
        entry = entry.strip()
        name: str | None = None

        eq_pos = entry.find("=")
        at_pos = entry.find("@")
        if 0 < at_pos < (eq_pos if eq_pos >= 0 else len(entry)):
            raw_name = entry[:at_pos].strip()
            if raw_name:
                name = raw_name
            entry = entry[at_pos + 1 :]

        pairs: list[tuple[str, str]] = []
        for part in entry.split("="):
            part = part.strip()
            if not part or ":" not in part:
                continue
            ch, cid = part.split(":", 1)
            ch = ch.strip()
            cid = cid.strip()
            if ch and cid:
                pairs.append((ch, cid))

        if len(pairs) < 2:
            raise ValueError(f"Entry must have at least 2 channel:id pairs: {entry!r}")

        return name, pairs

    @staticmethod
    def _generate_user_id(channel: ChannelType, channel_user_id: str | int) -> str:
        raw = f"{channel.value}:{channel_user_id}"
        return f"usr_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
