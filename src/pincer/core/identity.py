"""
Cross-channel identity resolver.

Maps channel-specific IDs (telegram:12345, whatsapp:491234567890, etc.) to a
unified pincer_user_id. Ensures the same memory and context persists across
all channels.

Schema
------
identity_profiles     — one row per pincer user (preferred channel, display name)
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
import dataclasses
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.channels.base import ChannelType
from pincer.db.engine import get_database_url
from pincer.services.identity import IdentityService, SeedEntry

if TYPE_CHECKING:
    from pincer.channels.base import BaseChannel

logger = logging.getLogger(__name__)

#: Names no identity may take: `/api/identity/me` is the caller's own identity.
RESERVED_IDENTITY_NAMES = frozenset({"me"})

# Channels whose IDs are phone numbers — leading "+" is stripped for storage
_PHONE_CHANNELS = {ChannelType.WHATSAPP, ChannelType.VOICE, ChannelType.SIGNAL}

_CHANNEL_VALUES = frozenset(c.value for c in ChannelType)


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
        self._seeded = asyncio.Event()
        self._service: IdentityService | None = None

    # Bounded wait for mark_seeding_complete() in is_guest(), so a future
    # caller of rebuild_identity_map() that doesn't guarantee it eventually
    # runs (e.g. skips the try/finally) hangs a channel handler for at most
    # this long instead of forever. Generous vs. the observed ~1-3s startup gap.
    _SEEDING_WAIT_TIMEOUT = 30.0

    @property
    def has_config(self) -> bool:
        return bool(self._identity_map_config)

    def mark_seeding_complete(self) -> None:
        """Signal that startup identity-map seeding (see seed_from_config) has finished.

        Until this is called, is_guest() blocks (rather than failing open) so
        channels don't treat an unresolved sender as trusted during the brief
        startup window before channel-specific IDs (e.g. WhatsApp LIDs) have
        been resolved against the configured identity map.
        """
        self._seeded.set()

    @property
    def _store(self) -> IdentityService:
        """The store, built on first use for callers that skip `ensure_table`."""
        if self._service is None:
            self._service = IdentityService(get_database_url(Path(self._db_path)))
        return self._service

    @staticmethod
    def _normalize_id(channel: ChannelType | str, channel_user_id: str | int) -> str:
        """Return a canonical string form of a channel user ID."""
        ch = ChannelType(channel) if isinstance(channel, str) else channel
        s = str(channel_user_id)
        return s.lstrip("+") if ch in _PHONE_CHANNELS else s

    async def ensure_table(self) -> None:
        """Ensure the identity schema is at head (see pincer.db.migrations)."""
        self._service = await IdentityService.for_path(Path(self._db_path))

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
        existing = await self._find_existing(channel, normalized)
        if existing:
            return existing
        return await self._check_config_mapping(channel, normalized)

    async def is_guest(self, channel: ChannelType, channel_user_id: str | int) -> bool:
        """True if an identity map is configured and this sender is not in it.

        No-op (always False) when no identity map is configured for the deployment.
        Blocks until startup seeding completes (see mark_seeding_complete) before
        evaluating, so a message arriving before seeding finishes is held rather
        than treated as trusted. Falls open after _SEEDING_WAIT_TIMEOUT if seeding
        never completes, so a stuck caller doesn't hang a channel handler forever.
        """
        if not self.has_config:
            return False
        if not await self._wait_for_seeding():
            return False
        return await self.find(channel, channel_user_id) is None

    async def _wait_for_seeding(self) -> bool:
        """Wait for startup seeding; False if it is still running after the timeout."""
        try:
            await asyncio.wait_for(self._seeded.wait(), timeout=self._SEEDING_WAIT_TIMEOUT)
        except TimeoutError:
            logger.error(
                "IdentityResolver: seeding still not complete after %.0fs — "
                "not waiting any longer, to avoid hanging the channel handler",
                self._SEEDING_WAIT_TIMEOUT,
            )
            return False
        return True

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
        existing = await self._find_existing(channel, normalized)
        if existing:
            return existing

        mapped = await self._check_config_mapping(channel, normalized)
        if mapped:
            return mapped

        pincer_user_id = self._generate_user_id(channel, normalized)
        await self._create_identity(pincer_user_id, channel, normalized, display_name)
        return pincer_user_id

    async def link_if_new(
        self,
        pincer_user_id: str,
        channel: ChannelType,
        channel_user_id: str | int,
    ) -> None:
        """Link channel_user_id to an existing pincer_user_id if not already linked."""
        normalized = self._normalize_id(channel, channel_user_id)
        if not await self._find_existing(channel, normalized):
            await self._link_channel(pincer_user_id, channel, normalized)

    async def promote_channel_id(
        self,
        channel: ChannelType,
        real_id: str | int,
        placeholder_ids: list[str],
    ) -> str | None:
        """Replace placeholder channel ids with the real one on the same identity.

        A WhatsApp contact whose LID was unknown at seeding is stored under
        their phone number; once a message reveals the LID, the phone row is
        swapped for it. Returns the owning identity, or None when there is none
        or the swap is refused.

        Owners are looked up with `_find_existing` only: `find()` falls back to
        the config mapping, which would re-create the placeholder row. Never
        creates an identity, and never merges two: a real id that already
        belongs to someone else is refused, as in `_claim_pairs`.

        Waits for startup seeding first, like `is_guest`: the placeholder may
        not be stored yet, so a timeout raises rather than finding nothing.
        """
        if self.has_config and not await self._wait_for_seeding():
            raise TimeoutError("identity seeding not complete")
        real = self._normalize_id(channel, real_id)
        real_owner = await self._find_existing(channel, real)
        linked: list[tuple[str, str]] = []
        for placeholder in placeholder_ids:
            normalized = self._normalize_id(channel, placeholder)
            if normalized == real:
                continue
            owner = await self._find_existing(channel, normalized)
            if owner:
                linked.append((normalized, owner))
        if not linked:
            return real_owner

        owners = {owner for _pid, owner in linked}
        if real_owner:
            owners.add(real_owner)
        if len(owners) > 1:
            logger.error(
                "Identity re-key refused: %s:%s and placeholder(s) %s belong to different identities (%s) — "
                "not merging them; fix the stored links",
                channel.value,
                real,
                ", ".join(f"{channel.value}:{pid}" for pid, _owner in linked),
                ", ".join(sorted(owners)),
            )
            return None

        owner = owners.pop()
        old_ids = [pid for pid, _owner in linked]
        taken_by = await self._store.replace_channel_links(owner, channel.value, real, old_ids)
        if taken_by is not None:
            logger.error(
                "Identity re-key refused: %s:%s was linked to %r while re-keying %r — not merging them",
                channel.value,
                real,
                taken_by,
                owner,
            )
            return None
        logger.info(
            "Identity re-keyed: %s %s → %s:%s",
            owner,
            ", ".join(f"{channel.value}:{pid}" for pid in old_ids),
            channel.value,
            real,
        )
        return owner

    async def _find_existing(self, channel: ChannelType, normalized_id: str) -> str | None:
        return await self._store.user_for_channel(channel.value, normalized_id)

    async def _create_identity(
        self,
        pincer_user_id: str,
        channel: ChannelType,
        normalized_id: str,
        display_name: str | None = None,
    ) -> None:
        await self._store.create_identity(
            pincer_user_id,
            channel=channel.value,
            channel_user_id=normalized_id,
            display_name=display_name,
        )
        logger.info("Identity created: %s (%s:%s)", pincer_user_id, channel.value, normalized_id)

    async def _link_channel(self, pincer_user_id: str, channel: ChannelType, normalized_id: str) -> None:
        await self._store.link_channel(pincer_user_id, channel.value, normalized_id)
        logger.info("Identity linked: %s ← %s:%s", pincer_user_id, channel.value, normalized_id)

    async def _rename_identity(self, old_id: str, new_id: str) -> None:
        """Rename a pincer_user_id across identity_profiles, channel_identities, and sessions.

        Only called for auto-generated hash IDs (usr_...) to avoid overwriting
        intentionally-named identities.
        """
        await self._store.rename_identity(old_id, new_id)
        logger.info(
            "Identity renamed: %s → %s "
            "(memory tags in MCP server still use %s — run 'pincer migrate-memories' to update)",
            old_id,
            new_id,
            old_id,
        )

    async def _check_config_mapping(self, channel: ChannelType, normalized_id: str) -> str | None:
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
                uid = await self._find_existing(ChannelType(ch), cid)
                if uid:
                    await self._link_channel(uid, channel, normalized_id)
                    return uid

            # No other side exists yet — establish a named identity now if configured
            if name:
                await self._create_identity(name, channel, normalized_id)
                return name

        return None

    async def _resolve_via_channel(
        self,
        ch_type: ChannelType,
        normalized_id: str,
        channels: dict[ChannelType, BaseChannel],
    ) -> str | None:
        """Ask a channel to translate a normalized config ID to its internal ID.

        Returns None when the translation fails or yields an empty ID: the
        pair is then left out rather than stored under a guess, since a wrong
        or shared ID links one person's channel to someone else.
        """
        channel = channels.get(ch_type)
        if channel is None:
            return normalized_id
        try:
            resolved = await channel.resolve_internal_user_id(normalized_id)
        except Exception as e:
            logger.error(
                "Channel %s: resolve_internal_user_id failed for %r: %s — leaving it out of the identity map",
                ch_type.value,
                normalized_id,
                e,
            )
            return None
        logger.info("Channel %s: resolve_internal_user_id success %r -> %r", ch_type.value, normalized_id, resolved)
        if not str(resolved).strip():
            # An empty ID would be shared by every unresolved contact and
            # fold them all into whichever identity claimed it first.
            logger.error(
                "Channel %s: resolve_internal_user_id returned an empty ID for %r — leaving it out of the identity map",
                ch_type.value,
                normalized_id,
            )
            return None
        return self._normalize_id(ch_type, resolved)

    async def _resolve_entry(
        self,
        raw_entry: str,
        pairs: list[tuple[str, str]],
        channels: dict[ChannelType, BaseChannel] | None,
        resolved_from: dict[tuple[str, str], str],
    ) -> tuple[list[tuple[str, ChannelType, str]], list[tuple[str, str]]]:
        """Resolve an entry's config IDs to internal IDs (phone → LID, etc.).

        Returns (resolved pairs, unresolved (channel, config ID) pairs). A
        pair is left out when its lookup fails, or when a different config
        ID already resolved to the same internal ID: that is a lookup error,
        not a shared pair. A config ID that is itself the internal ID it
        collides with (a phone in one entry, its LID in another) names the
        same account, so it counts as shared. `resolved_from` maps each
        resolved pair to the config ID that claimed it, across entries.
        """
        resolved: list[tuple[str, ChannelType, str]] = []
        unresolved: list[tuple[str, str]] = []
        for ch_str, cid in pairs:
            try:
                ch_type = ChannelType(ch_str)
            except ValueError:
                logger.warning("Unknown channel %r in identity map entry %r", ch_str, raw_entry.strip())
                continue
            config_id = self._normalize_id(ch_type, cid)
            norm = config_id
            if channels:
                internal = await self._resolve_via_channel(ch_type, config_id, channels)
                if internal is None:
                    unresolved.append((ch_str, config_id))
                    continue
                norm = internal
            source = resolved_from.setdefault((ch_type.value, norm), config_id)
            if source != config_id and norm not in (source, config_id):
                logger.error(
                    "Identity map: %s:%s and %s:%s both resolve to %s:%s — leaving %s:%s out",
                    ch_str,
                    source,
                    ch_str,
                    config_id,
                    ch_str,
                    norm,
                    ch_str,
                    config_id,
                )
                continue
            resolved.append((ch_str, ch_type, norm))
        return resolved, unresolved

    async def _rename_targets(
        self,
        resolved_entries: list[tuple[str | None, list[tuple[str, ChannelType, str]]]],
    ) -> set[str]:
        """Named identities a renamed config entry may keep seeding into.

        That is a stored named identity whose name the config no longer
        mentions and whose pairs only one entry touches. Two entries reaching
        the same old identity cannot both be its rename, and picking one
        would fold the other person into it.
        """
        configured_names = {name for name, _ in resolved_entries if name}
        touched_by: dict[str, int] = {}
        for _name, resolved in resolved_entries:
            owners: set[str] = set()
            for _ch_str, ch_type, norm in resolved:
                uid = await self._find_existing(ch_type, norm)
                if uid and not uid.startswith("usr_") and uid not in configured_names:
                    owners.add(uid)
            for uid in owners:
                touched_by[uid] = touched_by.get(uid, 0) + 1
        return {uid for uid, count in touched_by.items() if count == 1}

    async def _entry_owners(
        self,
        name: str | None,
        resolved: list[tuple[str, ChannelType, str]],
        pending: dict[tuple[str, str], str],
        rename_targets: set[str],
    ) -> tuple[list[tuple[tuple[str, ChannelType, str], str | None]], str | None]:
        """Each resolved pair with its current owner, and the entry's keeper.

        The keeper is the one named identity the entry seeds into: its own
        name; or, when the name owns none of the pairs and they all belong to
        one named identity in `rename_targets`, that identity (a rename keeps
        the existing identity, as it always has); or, for an unnamed entry,
        the first named owner.
        """
        owned: list[tuple[tuple[str, ChannelType, str], str | None]] = []
        for pair in resolved:
            _ch_str, ch_type, norm = pair
            uid = await self._find_existing(ch_type, norm) or pending.get((ch_type.value, norm))
            owned.append((pair, uid))
        named = list(dict.fromkeys(uid for _, uid in owned if uid and not uid.startswith("usr_")))

        keeper = name
        if (not name and named) or (
            name and len(named) == 1 and named[0] in rename_targets and await self._store.profile(name) is None
        ):
            keeper = named[0]
        return owned, keeper

    async def _claim_pairs(
        self,
        name: str | None,
        resolved: list[tuple[str, ChannelType, str]],
        pending: dict[tuple[str, str], str],
        rename_targets: set[str],
    ) -> tuple[list[tuple[str, ChannelType, str]], str | None, list[str]]:
        """Decide which identity an entry seeds into and which pairs it keeps.

        Returns (kept pairs, keeper, existing owners of the kept pairs); see
        `_entry_owners` for the keeper. A pair owned by any other named
        identity is dropped: folding one named person into another would
        silently share their memory and sessions. Unnamed (usr_…) owners are
        kept and merged.
        """
        owned, keeper = await self._entry_owners(name, resolved, pending, rename_targets)
        if name and keeper != name:
            logger.warning("Named canonical ID %r ignored: identity already has name %r", name, keeper)

        kept: list[tuple[str, ChannelType, str]] = []
        existing_uids: list[str] = []
        joined = False
        for pair, uid in owned:
            ch_str, _ch_type, norm = pair
            if uid and not uid.startswith("usr_") and uid != keeper:
                logger.error(
                    "Identity map: %s:%s is configured with %r but already belongs to %r — "
                    "not merging them; fix the config or the stored link",
                    ch_str,
                    norm,
                    keeper,
                    uid,
                )
                continue
            kept.append(pair)
            joined = joined or uid != keeper
            if uid and uid not in existing_uids:
                existing_uids.append(uid)
        if not name and keeper and joined:
            # Only a shared pair ties this entry to the keeper; a wrong lookup
            # landing on a pair the keeper lists looks the same, so say so.
            logger.warning(
                "Identity map: unnamed entry %s joins %r through a pair they share — "
                "give the entry a name if this is a different person",
                "=".join(f"{ch_str}:{norm}" for ch_str, _ch_type, norm in kept),
                keeper,
            )
        return kept, keeper, existing_uids

    async def cleanup(
        self,
        channels: dict[ChannelType, BaseChannel] | None = None,
    ) -> None:
        """Remove stale rows from the identity tables.

        If PINCER_IDENTITY_MAP is configured, the resolved internal IDs for
        every channel pair are the authoritative whitelist. Any
        channel_identities row whose (channel, channel_user_id) is NOT in the
        whitelist is deleted, and any identity_profiles row left with no channels is
        deleted with it. Passing ``channels`` allows config IDs to be translated
        to real internal IDs before the whitelist is built, the same way
        seed_from_config does (see `_resolve_entry`).

        When no config is set the method is a no-op (nothing to enforce), and
        so it is when any entry does not parse or names an unknown channel: a
        whitelist with a hole in it must not be enforced. A pair whose lookup fails is a smaller hole,
        filled by `_unresolved_keep`.
        """
        if not self._identity_map_config:
            return

        parsed: list[tuple[str, str | None, list[tuple[str, str]]]] = []
        for raw_entry in self._identity_map_config.split(","):
            if not raw_entry.strip():
                continue
            try:
                name, pairs = self._parse_mapping(raw_entry)
            except ValueError as e:
                # Its channels are unknown, so the whitelist is incomplete:
                # pruning against it would delete the very identity the
                # entry was meant to keep.
                logger.error("Identity map entry %r is invalid (%s) — skipping cleanup", raw_entry.strip(), e)
                return
            if any(ch not in _CHANNEL_VALUES for ch, _cid in pairs):
                # Likely a typo for a real channel (`WhatsApp:4911`): the pair's
                # stored link would be pruned, and with it a one-channel identity.
                logger.error("Identity map entry %r names an unknown channel — skipping cleanup", raw_entry.strip())
                return
            parsed.append((raw_entry, name, pairs))

        resolved_from: dict[tuple[str, str], str] = {}
        entries: list[tuple[str | None, list[tuple[str, ChannelType, str]], list[tuple[str, str]]]] = []
        for raw_entry, name, pairs in parsed:
            resolved, unresolved = await self._resolve_entry(raw_entry, pairs, channels, resolved_from)
            entries.append((name, resolved, unresolved))
        allowed = {(ch_str, norm) for _name, resolved, _unresolved in entries for ch_str, _ch_type, norm in resolved}
        if any(unresolved for _name, _resolved, unresolved in entries):
            allowed |= await self._unresolved_keep(entries)

        if not allowed:
            logger.error(
                "Identity config produced no usable channel:id pairs — "
                "skipping cleanup to avoid wiping the identity tables"
            )
            return

        try:
            unlisted, channelless = await self._store.prune(allowed)
        except Exception:
            # A database without the identity tables has nothing to clean.
            logger.debug("Identity cleanup skipped: tables unavailable", exc_info=True)
            return

        if unlisted or channelless:
            logger.info(
                "Identity cleanup: removed %d unlisted channel link(s), %d channelless identity/identities",
                unlisted,
                channelless,
            )

    async def _unresolved_keep(
        self,
        entries: list[tuple[str | None, list[tuple[str, ChannelType, str]], list[tuple[str, str]]]],
    ) -> set[tuple[str, str]]:
        """Stored pairs cleanup must keep for the entries' unresolved pairs.

        The link an unresolved pair stands for (e.g. a LID learned at runtime)
        is unknown, but it belongs to the entry's own identity: its keeper
        (see `_entry_owners`) and the unnamed identities seeding merges into
        it. That identity keeps its links on the channel. A named identity
        whose pair the entry is refused (see `_claim_pairs`) is not part of it.

        With no known identity, the link is unlinked or owned by an unnamed
        identity (a named owner would be the entry's own name), so only
        unnamed identities and the identities the config still names keep
        their links on that channel. Removed named people are still pruned.
        """
        rename_targets = await self._rename_targets([(name, resolved) for name, resolved, _ in entries])
        identities: list[set[str]] = []
        for name, resolved, _unresolved in entries:
            owned, keeper = await self._entry_owners(name, resolved, {}, rename_targets)
            identity = {keeper} if keeper else set()
            identity.update(uid for _pair, uid in owned if uid and uid.startswith("usr_"))
            identities.append(identity)
        configured = {name for name, _resolved, _unresolved in entries if name}.union(*identities)

        keep: set[tuple[str, str]] = set()
        for (_name, _resolved, unresolved), identity in zip(entries, identities, strict=True):
            if not unresolved:
                continue
            links: set[tuple[str, str]] = set()
            for uid in identity:
                links.update(await self._store.channels_for(uid))
            for ch, config_id in unresolved:
                if links:
                    logger.error(
                        "Identity map pair %s:%s did not resolve — keeping the %s links of %s",
                        ch,
                        config_id,
                        ch,
                        ", ".join(sorted(identity)),
                    )
                    keep.update(pair for pair in links if pair[0] == ch)
                else:
                    logger.error(
                        "Identity map pair %s:%s did not resolve and its identity is unknown — "
                        "keeping the %s links of unnamed and configured identities",
                        ch,
                        config_id,
                        ch,
                    )
                    keep.update(
                        (ch, cid)
                        for cid, uid in await self._store.owners_on(ch)
                        if uid.startswith("usr_") or uid in configured
                    )
        return keep

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

        entries: list[SeedEntry] = []
        # Every link is written in one transaction after the loop, so an entry
        # cannot find the pairs an earlier entry claimed in the database; it
        # finds them here instead, or one person sharing a channel pair across
        # two entries would be split into two identities.
        pending: dict[tuple[str, str], str] = {}
        # Resolved pair → the config ID it came from (see _resolve_entry).
        resolved_from: dict[tuple[str, str], str] = {}

        async def rename(old_uid: str, new_uid: str) -> None:
            await self._rename_identity(old_uid, new_uid)
            for key, uid in pending.items():
                if uid == old_uid:
                    pending[key] = new_uid
            entries[:] = [
                dataclasses.replace(entry, pincer_user_id=new_uid) if entry.pincer_user_id == old_uid else entry
                for entry in entries
            ]

        parsed: list[tuple[str, str | None, list[tuple[str, str]]]] = []
        for raw_entry in self._identity_map_config.split(","):
            if ":" not in raw_entry:
                continue
            try:
                name, pairs = self._parse_mapping(raw_entry)
            except ValueError:
                logger.warning("Invalid identity map entry: %r", raw_entry.strip())
                continue
            parsed.append((raw_entry, name, pairs))
        resolved_entries = [
            (name, (await self._resolve_entry(raw_entry, pairs, channels, resolved_from))[0])
            for raw_entry, name, pairs in parsed
        ]
        rename_targets = await self._rename_targets(resolved_entries)

        for name, resolved in resolved_entries:
            resolved, keeper, existing_uids = await self._claim_pairs(name, resolved, pending, rename_targets)
            if not resolved:
                continue

            # Determine or create the canonical pincer_user_id. Every existing
            # identity but the keeper is unnamed (see _claim_pairs) and is
            # renamed/merged into it.
            if existing_uids:
                pincer_uid = keeper or existing_uids[0]
                for uid in existing_uids:
                    if uid != pincer_uid:
                        if len(existing_uids) > 1:
                            logger.info("Identity conflict resolved: merging %s into %s", uid, pincer_uid)
                        await rename(uid, pincer_uid)
            elif keeper:
                pincer_uid = keeper
            else:
                _first_ch_str, first_ch_type, first_norm = resolved[0]
                pincer_uid = self._generate_user_id(first_ch_type, first_norm)

            first_channel = resolved[0][0]
            # A renamed entry seeds into the old identity, configured under the new name.
            profile = self._profiles.get(pincer_uid) or self._profiles.get(name or "") or IdentityProfile()
            # `preferred_channel` is write-once at creation; day-to-day channel
            # use is tracked by `active_channel` (see touch_active_channel).
            config_fields: dict[str, Any] = {
                "display_name": profile.display_name,
                "preferred_channel": profile.preferred_channel,
                "email": profile.email,
                "timezone": profile.timezone,
            }
            entries.append(
                SeedEntry(
                    pincer_user_id=pincer_uid,
                    channels=[(ch_str, norm) for ch_str, _ch_type, norm in resolved],
                    insert={**config_fields, "preferred_channel": profile.preferred_channel or first_channel},
                    backfill=config_fields,
                )
            )
            for _ch_str, ch_type, norm in resolved:
                pending.setdefault((ch_type.value, norm), pincer_uid)

        await self._store.seed(entries)
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
        await self._store.touch_active_channel(pincer_user_id, channel.value)

    async def get_timezone(self, pincer_user_id: str) -> str:
        """This identity's IANA timezone, or "" when none is configured.

        Set from a `[identity.<key>] timezone` entry (see config/identity.py).
        The caller decides the fallback — an empty string here means "no
        opinion", not UTC, so a deployment-wide `settings.timezone` still wins
        over a hardcoded default.
        """
        profile = await self._store.profile(pincer_user_id)
        timezone = profile.get("timezone") if profile else None
        return str(timezone).strip() if timezone else ""

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
        meta = await self._store.profile(pincer_user_id)
        if not meta:
            raise ValueError(f"Unknown user: {pincer_user_id}")

        active = meta["active_channel"]
        preferred = meta["preferred_channel"]
        all_channels = await self._get_all_channels_raw(pincer_user_id)

        active_fresh = self._is_within_age(meta["active_channel_updated_at"], max_active_age_seconds)
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
        raw = await self._get_all_channels_raw(pincer_user_id)
        return {ChannelType(ch): cid for ch, cid in raw.items()}

    async def _get_all_channels_raw(self, pincer_user_id: str) -> dict[str, str]:
        """Return first seen channel_user_id per channel for this user."""
        result: dict[str, str] = {}
        for channel, channel_user_id in await self._store.channels_for(pincer_user_id):
            result.setdefault(channel, channel_user_id)
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

        Raises ValueError if no channel:id pairs are found, or if the name is
        reserved (see RESERVED_IDENTITY_NAMES).
        """
        entry = entry.strip()
        name: str | None = None

        eq_pos = entry.find("=")
        at_pos = entry.find("@")
        if 0 < at_pos < (eq_pos if eq_pos >= 0 else len(entry)):
            raw_name = entry[:at_pos].strip()
            if raw_name.lower() in RESERVED_IDENTITY_NAMES:
                raise ValueError(f"Identity name {raw_name!r} is reserved")
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
