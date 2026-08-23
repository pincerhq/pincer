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

Single channel — no linking needed, just registers one identity explicitly
(useful to name/allowlist a user without requiring a second channel):
    PINCER_IDENTITY_MAP=john@telegram:johnDoe

Multiple channels per identity and multiple identities separated by commas:
    PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890=signal:491234567890,jane@telegram:janeAustin=whatsapp:491111111111

The optional name prefix sets the pincer_user_id directly (e.g. "user:john" in
memory tags) instead of the auto-generated "user:usr_abc123..." hash.
Memory stored under a named ID is portable across DB rebuilds as long as the
config entry stays the same.

Config mapping (pincer.toml / pincer.local.toml)
-------------------------------------------------
For rosters too large to comfortably manage as one PINCER_IDENTITY_MAP
string, the same mapping can be expressed as a `[identity]` TOML section
instead — see `pincer.config.identity` for the schema. PINCER_IDENTITY_MAP,
when set, always takes full precedence over the TOML section (no merging
between the two). `pincer.config.identity.resolve_identity_map_config`
converts TOML entries into two things: this module's own string grammar
(channel-linking, same as PINCER_IDENTITY_MAP) and a `pincer_user_id ->
IdentityProfile` dict for the fields that string grammar has no room for
(display_name, preferred_channel, email, timezone) — see `IdentityProfile`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from pincer.channels.base import ChannelType
from pincer.db import ensure_schema_current

if TYPE_CHECKING:
    from pincer.channels.base import BaseChannel

logger = logging.getLogger(__name__)

# Channels whose IDs are phone numbers — leading "+" is stripped for storage
_PHONE_CHANNELS = {ChannelType.WHATSAPP, ChannelType.VOICE, ChannelType.SIGNAL}


@dataclass(frozen=True)
class IdentityProfile:
    """Optional profile fields sourced from the [identity] TOML section (see
    `pincer.config.identity`) — PINCER_IDENTITY_MAP's string grammar has no
    field for any of these, so entries seeded from the env var never get a
    profile and these all stay unset. All fields are optional; a profile
    with everything None is valid (e.g. a TOML person entry that only sets
    channels).
    """

    display_name: str | None = None
    preferred_channel: str | None = None
    email: str | None = None
    timezone: str | None = None


class IdentityResolver:
    """Resolves channel-specific user IDs to a unified Pincer user ID."""

    def __init__(
        self,
        db_path: Path,
        identity_map_config: str = "",
        profiles: dict[str, IdentityProfile] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._identity_map_config = identity_map_config
        self._profiles = profiles or {}

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
        """Ensure the identity schema is at head (see pincer.db.migrations)."""
        await asyncio.to_thread(ensure_schema_current, Path(self._db_path))

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
        """Check PINCER_IDENTITY_MAP for a pre-configured identity or cross-channel link."""
        if not self._identity_map_config:
            return None

        current_key = f"{channel.value}:{normalized_id}"

        for raw_entry in self._identity_map_config.split(","):
            if ":" not in raw_entry:
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
            if ":" not in raw_entry:
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

        if not allowed:
            logger.error(
                "Identity config produced no usable channel:id pairs — "
                "skipping cleanup to avoid wiping the identity tables"
            )
            return

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
                if ":" not in raw_entry:
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
                profile = self._profiles.get(pincer_uid, IdentityProfile())
                preferred_channel = profile.preferred_channel or first_channel
                await db.execute(
                    "INSERT OR IGNORE INTO identity_meta "
                    "(pincer_user_id, preferred_channel, display_name, email, timezone) VALUES (?, ?, ?, ?, ?)",
                    (pincer_uid, preferred_channel, profile.display_name, profile.email, profile.timezone),
                )
                if profile != IdentityProfile():
                    # INSERT OR IGNORE above is a no-op for an identity that already
                    # existed (e.g. created earlier via resolve()); COALESCE here
                    # backfills profile fields onto it without clobbering an
                    # existing value with NULL when only some are set. preferred_channel
                    # is otherwise write-once (see touch_active_channel's docstring for
                    # why active_channel, not this, tracks day-to-day channel use).
                    await db.execute(
                        "UPDATE identity_meta SET "
                        "display_name = COALESCE(?, display_name), "
                        "preferred_channel = COALESCE(?, preferred_channel), "
                        "email = COALESCE(?, email), "
                        "timezone = COALESCE(?, timezone) "
                        "WHERE pincer_user_id = ?",
                        (profile.display_name, profile.preferred_channel, profile.email, profile.timezone, pincer_uid),
                    )
                for ch_str, _ch_type, norm in resolved:
                    await db.execute(
                        "INSERT OR IGNORE INTO channel_identities "
                        "(channel, channel_user_id, pincer_user_id) VALUES (?, ?, ?)",
                        (ch_str, norm, pincer_uid),
                    )

            await db.commit()
            logger.info("Identity config seeded")

    async def touch_active_channel(self, pincer_user_id: str, channel: ChannelType) -> None:
        """Record `channel` as this identity's most-recently-active channel.

        Called on every inbound message (see `IdentityMiddleware`), regardless
        of which channel it came in on. Always writes `active_channel_updated_at`,
        even when `active_channel` itself hasn't changed — the timestamp tracks
        "last seen active", not "last changed channel". A user who messages
        continuously on the same channel for hours must keep that timestamp
        current, or `get_preferred_channel`'s `max_active_age_seconds` staleness
        check would wrongly treat them as inactive and fall back to
        `preferred_channel`.
        """
        async with self._get_db() as db:
            await db.execute(
                "UPDATE identity_meta SET active_channel = ?, active_channel_updated_at = datetime('now') "
                "WHERE pincer_user_id = ?",
                (channel.value, pincer_user_id),
            )
            await db.commit()

    async def get_preferred_channel(
        self,
        pincer_user_id: str,
        max_active_age_seconds: float | None = None,
    ) -> tuple[ChannelType, str]:
        """Get user's channel for proactive messages.

        Prefers `active_channel` (the channel that most recently sent a
        message for this identity — see `touch_active_channel`) over the
        write-once `preferred_channel` set at identity creation, since for a
        multi-channel identity (linked via PINCER_IDENTITY_MAP) the latter
        goes stale the moment the user messages from a different linked
        channel than the one that originally created the identity row.

        If `max_active_age_seconds` is given, `active_channel` is only used
        when `active_channel_updated_at` is within that window of now —
        otherwise falls through to `preferred_channel`. Meant for delayed
        notifications (e.g. an MCP server's background task completing well
        after the message that triggered it): a channel the user was active
        on half an hour ago may no longer be the right place to reach them,
        so a stale `active_channel` is worse than the durable fallback.
        """
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT preferred_channel, active_channel, active_channel_updated_at "
                "FROM identity_meta WHERE pincer_user_id = ?",
                (pincer_user_id,),
            )
            meta = await cursor.fetchone()
            if not meta:
                raise ValueError(f"Unknown user: {pincer_user_id}")

            active = meta["active_channel"]
            active_updated_at = meta["active_channel_updated_at"]
            preferred = meta["preferred_channel"]
            all_channels = await self._get_all_channels_raw(db, pincer_user_id)

            active_fresh = self._is_within_age(active_updated_at, max_active_age_seconds)
            if active and active in all_channels and active_fresh:
                return ChannelType(active), all_channels[active]

            if preferred and preferred in all_channels:
                return ChannelType(preferred), all_channels[preferred]

            for ch_name, ch_id in all_channels.items():
                return ChannelType(ch_name), ch_id

            raise ValueError(f"No channels linked for user: {pincer_user_id}")

    @staticmethod
    def _is_within_age(timestamp: str | None, max_age_seconds: float | None) -> bool:
        """True if `timestamp` (a SQLite `datetime('now')` UTC string) is within `max_age_seconds` of now.

        No age limit (`max_age_seconds is None`) always passes. A limit with a
        missing/unparseable timestamp always fails — treat "unknown age" as stale.
        """
        if max_age_seconds is None:
            return True
        if not timestamp:
            return False
        try:
            ts = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
        except ValueError:
            return False
        return (datetime.now(UTC) - ts).total_seconds() <= max_age_seconds

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
            name@ch1:id1                    (named canonical ID, single channel)
            name@ch1:id1=ch2:id2=ch3:id3   (named canonical ID, N channels)
            ch1:id1=ch2:id2                 (hash-based, backward compat)

        Raises ValueError if no channel:id pairs are found.
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

        if not pairs:
            raise ValueError(f"Entry must have at least 1 channel:id pair: {entry!r}")

        return name, pairs

    @staticmethod
    def _generate_user_id(channel: ChannelType, channel_user_id: str | int) -> str:
        raw = f"{channel.value}:{channel_user_id}"
        return f"usr_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
