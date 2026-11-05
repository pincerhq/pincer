"""
Cross-channel identity resolver.

Maps channel-specific IDs (telegram:12345, whatsapp:491234567890, etc.) to a
unified pincer_user_id. Ensures the same memory and context persists across
all channels.

Schema
------
identity_profiles  — one row per pincer user (preferred channel, display name,
                     `active`, `source`)
channel_identities — many-to-many: (channel, channel_user_id) → pincer_user_id

Where identities come from
--------------------------
- The `[identity]` section of pincer.toml / pincer.local.toml (see
  `pincer.config.identity` for the schema). Each `[identity.<key>]` names one
  person; `<key>` is the pincer_user_id. How the section is applied at start
  is `PINCER_IDENTITY_MANAGEMENT` (`sync`, `update`, `clionly`); see
  `seed_from_config` and `cleanup`.
- `pincer identity …`, in every mode.
- `resolve()`, which makes an auto `usr_…` identity for an unknown sender when
  the guest gate is off.

A deactivated identity (`active` false) is not recognised anywhere: its
channel ids and phones identify nobody, so its senders are guests. Every
lookup reads the database, so (de)activating takes effect in a running app.

The guest gate (`gated`) is on when the mode is `clionly`, the TOML lists
anyone, or the database has a configured or hand-made identity with a
channel (a deactivated one included: it is a guest, not a reason to open
the gate).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.channels.base import ChannelType
from pincer.db.engine import get_database_url
from pincer.services.identity import SOURCE_CONFIG, IdentityService, SeedEntry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pincer.channels.base import BaseChannel
    from pincer.config.identity import IdentityTomlEntry

logger = logging.getLogger(__name__)

#: Names no identity may take: `/api/identity/me` is the caller's own identity.
RESERVED_IDENTITY_NAMES = frozenset({"me"})

# Channels whose IDs are phone numbers — leading "+" is stripped for storage
_PHONE_CHANNELS = {ChannelType.WHATSAPP, ChannelType.VOICE, ChannelType.SIGNAL}

# A resolved config pair: (channel value, channel type, normalized internal id).
_Pair = tuple[str, ChannelType, str]


class IdentityResolver:
    """Resolves channel-specific user IDs to a unified Pincer user ID.

    `entries` are the `[identity]` TOML people (`pincer.config.identity.
    load_identity_map`), already empty in `clionly`; `mode` is
    `PINCER_IDENTITY_MANAGEMENT`.
    """

    def __init__(
        self,
        db_path: Path,
        entries: Mapping[str, IdentityTomlEntry] | None = None,
        mode: str = "update",
    ) -> None:
        self._db_path = str(db_path)
        self._entries: dict[str, IdentityTomlEntry] = dict(entries or {})
        self._mode = mode
        self._seeded = asyncio.Event()
        self._service: IdentityService | None = None
        # Whether the database holds a roster (see `gated`), and when it was last asked.
        self._roster = False
        self._roster_checked: float | None = None

    # Bounded wait for mark_seeding_complete() in is_guest(), so a future
    # caller of rebuild_identity_map() that doesn't guarantee it eventually
    # runs (e.g. skips the try/finally) hangs a channel handler for at most
    # this long instead of forever. Generous vs. the observed ~1-3s startup gap.
    _SEEDING_WAIT_TIMEOUT = 30.0

    # How long `gated` trusts its last look at the database. Short, so the
    # first `pincer identity create … --channel` closes the gate in a running
    # app within seconds; long enough that a busy channel does not ask per message.
    _ROSTER_TTL = 5.0

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def entries(self) -> Mapping[str, IdentityTomlEntry]:
        return self._entries

    async def gated(self) -> bool:
        """True when unknown senders are guests (the guest gate is on).

        That is when the mode is `clionly`, the TOML lists anyone, or the
        database has a configured or hand-made identity with a channel
        (active or not) — so a roster kept only in the database still gates.

        Fails closed: while the database cannot be asked, the gate is on.
        """
        if self._mode == "clionly" or self._entries:
            return True
        stale = self._roster_checked is None or time.monotonic() - self._roster_checked >= self._ROSTER_TTL
        if stale and not await self.refresh_gate():
            return True
        return self._roster

    async def refresh_gate(self) -> bool:
        """Ask the database again whether it holds a roster (see `gated`).

        False when the question failed; nothing is cached then, so the next
        call asks again rather than trusting a stale or unknown answer.
        """
        try:
            self._roster = await self._store.has_roster()
        except Exception:
            logger.warning("Identity roster check failed — treating unknown senders as guests", exc_info=True)
            self._roster_checked = None
            return False
        self._roster_checked = time.monotonic()
        return True

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

        Returns the pincer_user_id if found via DB or config mapping, else
        None. A deactivated identity is never found.
        """
        normalized = self._normalize_id(channel, channel_user_id)
        existing = await self._find_existing(channel, normalized)
        if existing:
            return existing
        return await self._check_config_mapping(channel, normalized)

    async def is_guest(self, channel: ChannelType, channel_user_id: str | int) -> bool:
        """True if the guest gate is on (see `gated`) and this sender is not a known, active identity.

        No-op (always False) when the gate is off.
        Blocks until startup seeding completes (see mark_seeding_complete) before
        evaluating, so a message arriving before seeding finishes is held rather
        than treated as trusted. Falls open after _SEEDING_WAIT_TIMEOUT if seeding
        never completes, so a stuck caller doesn't hang a channel handler forever.
        """
        if not await self.gated():
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
        2. Pre-configured cross-channel link from the [identity] TOML
        3. Create new identity

        A channel id that belongs to a deactivated identity resolves to the
        channel-scoped id (`telegram:123`), the same as an unknown sender
        behind the guest gate, and nothing is created or linked for it.
        """
        normalized = self._normalize_id(channel, channel_user_id)
        existing = await self._find_existing(channel, normalized)
        if existing:
            return existing
        if await self._store.owner_any(channel.value, normalized) is not None:
            return f"{channel.value}:{normalized}"

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
        if not await self.is_active(pincer_user_id):
            # No profile (e.g. a channel-scoped stand-in id) or a deactivated
            # one: a link would point nowhere, or hand a guest's id to it.
            return
        if await self._store.owner_any(channel.value, normalized) is None:
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
        if await self.gated() and not await self._wait_for_seeding():
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
        """The active identity this id is linked to."""
        return await self._store.user_for_channel(channel.value, normalized_id)

    async def _find_owner(self, channel: ChannelType, normalized_id: str) -> str | None:
        """The identity this id is linked to, active or not — for seeding and
        cleanup, which must not take a deactivated person's link for a free one."""
        owner = await self._store.owner_any(channel.value, normalized_id)
        return owner[0] if owner else None

    async def is_active(self, pincer_user_id: str) -> bool:
        """True for an existing identity that is not deactivated."""
        profile = await self._store.profile(pincer_user_id)
        return bool(profile and profile.get("active"))

    async def _create_identity(
        self,
        pincer_user_id: str,
        channel: ChannelType,
        normalized_id: str,
        display_name: str | None = None,
        source: str | None = None,
    ) -> None:
        await self._store.create_identity(
            pincer_user_id,
            channel=channel.value,
            channel_user_id=normalized_id,
            display_name=display_name,
            source=source,
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
        """Check the [identity] TOML for a pre-configured identity or cross-channel link.

        Nothing is linked or created for a deactivated person: when the id,
        the entry's name or another of its channels belongs to one, the
        sender stays unknown.
        """
        if not self._entries:
            return None
        current = (channel.value, normalized_id)
        if await self._store.owner_any(*current) is not None:
            # Linked, but not to an active identity (that one `_find_existing` found).
            return None

        for name, entry in self._entries.items():
            norm_pairs = [(ch, self._normalize_id(ch, cid)) for ch, cid in entry.channels]
            if current not in norm_pairs:
                continue
            profile = await self._store.profile(name)
            if profile is not None and not profile.get("active"):
                return None

            # Look for an existing identity among the other channels in this entry
            for ch, cid in norm_pairs:
                if (ch, cid) == current:
                    continue
                owner = await self._store.owner_any(ch, cid)
                if owner is None:
                    continue
                uid, active = owner
                if not active:
                    return None
                await self._link_channel(uid, channel, normalized_id)
                return uid

            # No other side exists yet — establish the named identity now
            await self._create_identity(name, channel, normalized_id, source=SOURCE_CONFIG)
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
        name: str,
        pairs: list[tuple[str, str]],
        channels: dict[ChannelType, BaseChannel] | None,
        resolved_from: dict[tuple[str, str], str],
    ) -> tuple[list[_Pair], list[tuple[str, str]]]:
        """Resolve an entry's config IDs to internal IDs (phone → LID, etc.).

        Returns (resolved pairs, unresolved (channel, config ID) pairs). A
        pair is left out when its lookup fails, or when a different config
        ID already resolved to the same internal ID: that is a lookup error,
        not a shared pair. A config ID that is itself the internal ID it
        collides with (a phone in one entry, its LID in another) names the
        same account, so it counts as shared. `resolved_from` maps each
        resolved pair to the config ID that claimed it, across entries.
        """
        resolved: list[_Pair] = []
        unresolved: list[tuple[str, str]] = []
        for ch_str, cid in pairs:
            try:
                ch_type = ChannelType(ch_str)
            except ValueError:
                logger.warning("Unknown channel %r in identity.%s", ch_str, name)
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

    async def _rename_targets(self, resolved_entries: list[tuple[str, list[_Pair]]]) -> set[str]:
        """Named identities a renamed config entry may keep seeding into.

        That is a stored named identity whose name the config no longer
        mentions and whose pairs only one entry touches. Two entries reaching
        the same old identity cannot both be its rename, and picking one
        would fold the other person into it.

        Only an identity that came from the config can be renamed by it. One
        made with the CLI is somebody's on purpose: a TOML entry claiming its
        channel is refused that pair (see `_claim_pairs`), not merged into it.
        """
        configured_names = {name for name, _ in resolved_entries}
        touched_by: dict[str, int] = {}
        for _name, resolved in resolved_entries:
            owners: set[str] = set()
            for _ch_str, ch_type, norm in resolved:
                uid = await self._find_owner(ch_type, norm)
                if uid and not uid.startswith("usr_") and uid not in configured_names:
                    owners.add(uid)
            for uid in owners:
                touched_by[uid] = touched_by.get(uid, 0) + 1
        targets: set[str] = set()
        for uid, count in touched_by.items():
            profile = await self._store.profile(uid) if count == 1 else None
            if profile is not None and profile.get("source") == SOURCE_CONFIG:
                targets.add(uid)
        return targets

    async def _entry_owners(
        self,
        name: str,
        resolved: list[_Pair],
        pending: dict[tuple[str, str], str],
        rename_targets: set[str],
    ) -> tuple[list[tuple[_Pair, str | None]], str]:
        """Each resolved pair with its current owner, and the entry's keeper.

        The keeper is the one named identity the entry seeds into: its own
        name; or, when the name owns none of the pairs and they all belong to
        one named identity in `rename_targets`, that identity (a rename keeps
        the existing identity, as it always has).
        """
        owned: list[tuple[_Pair, str | None]] = []
        for pair in resolved:
            _ch_str, ch_type, norm = pair
            uid = await self._find_owner(ch_type, norm) or pending.get((ch_type.value, norm))
            owned.append((pair, uid))
        named = list(dict.fromkeys(uid for _, uid in owned if uid and not uid.startswith("usr_")))

        keeper = name
        if len(named) == 1 and named[0] in rename_targets and await self._store.profile(name) is None:
            keeper = named[0]
        return owned, keeper

    async def _claim_pairs(
        self,
        name: str,
        resolved: list[_Pair],
        pending: dict[tuple[str, str], str],
        rename_targets: set[str],
    ) -> tuple[list[_Pair], str, list[str]]:
        """Decide which identity an entry seeds into and which pairs it keeps.

        Returns (kept pairs, keeper, existing owners of the kept pairs); see
        `_entry_owners` for the keeper. A pair owned by any other named
        identity is dropped: folding one named person into another would
        silently share their memory and sessions. Unnamed (usr_…) owners are
        kept and merged.
        """
        owned, keeper = await self._entry_owners(name, resolved, pending, rename_targets)
        if keeper != name:
            logger.warning("Named canonical ID %r ignored: identity already has name %r", name, keeper)

        kept: list[_Pair] = []
        existing_uids: list[str] = []
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
            if uid and uid not in existing_uids:
                existing_uids.append(uid)
        return kept, keeper, existing_uids

    async def _resolved_entries(
        self, channels: dict[ChannelType, BaseChannel] | None
    ) -> list[tuple[str, list[_Pair], list[tuple[str, str]]]]:
        """Every entry as (name, resolved pairs, unresolved pairs); see `_resolve_entry`."""
        resolved_from: dict[tuple[str, str], str] = {}
        entries: list[tuple[str, list[_Pair], list[tuple[str, str]]]] = []
        for name, entry in self._entries.items():
            resolved, unresolved = await self._resolve_entry(name, list(entry.channels), channels, resolved_from)
            entries.append((name, resolved, unresolved))
        return entries

    async def cleanup(
        self,
        channels: dict[ChannelType, BaseChannel] | None = None,
    ) -> None:
        """Remove the links and identities the TOML no longer has, as the mode says.

        The resolved internal IDs of every TOML channel pair form a
        whitelist. Passing ``channels`` translates config IDs to real
        internal IDs first, the same way seed_from_config does (see
        `_resolve_entry`); a pair whose lookup fails is filled in by
        `_unresolved_keep`.

        - `sync`: a link outside the whitelist is removed when its owner came
          from the config or is an auto `usr_` identity, and such identities
          left with no channel are deleted. Identities made with the CLI keep
          everything.
        - `update`: only the TOML-listed identities lose the links the TOML
          no longer gives them. Nobody else is touched, and nobody is deleted.
        - `clionly`: nothing to enforce.

        With no entries at all, `sync` removes every identity that came from
        the config (auto `usr_` ids and CLI ones stay), and `update` warns
        about config identities still in the database, which keep the guest
        gate on (see `_no_entries`).

        Phones are not handled here but by `cleanup_phones`, which startup
        runs on its own so a failure in seeding or here cannot skip it.
        """
        if self._mode == "clionly":
            return
        if not self._entries:
            await self._no_entries()
            return

        entries = await self._resolved_entries(channels)
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
            listed = None if self._mode == "sync" else await self._listed_identities(entries)
            unlisted, channelless = await self._store.prune(allowed, listed=listed)
        except Exception:
            # A database without the identity tables has nothing to clean.
            logger.debug("Identity cleanup skipped: tables unavailable", exc_info=True)
            return

        if unlisted or channelless:
            logger.info(
                "Identity cleanup (%s): removed %d unlisted channel link(s), %d channelless identity/identities",
                self._mode,
                unlisted,
                channelless,
            )

    async def _no_entries(self) -> None:
        """Cleanup for a TOML that lists nobody.

        In `sync`, the TOML is the source of truth, so the config identities
        go. In `update` nothing is removed, but config identities left from an
        earlier config — or backfilled as config by migration 0028 — keep the
        guest gate on, which nothing else in the log would explain.
        """
        try:
            if self._mode == "sync":
                unlisted, channelless = await self._store.prune(set(), sources=(SOURCE_CONFIG,))
                if unlisted or channelless:
                    logger.warning(
                        "Identity cleanup (sync): the [identity] TOML lists nobody — removed %d link(s) and "
                        "%d identity/identities that came from the config",
                        unlisted,
                        channelless,
                    )
                return
            leftovers = await self._store.ids_by_source(SOURCE_CONFIG, linked_only=True)
        except Exception:
            logger.debug("Identity cleanup skipped: tables unavailable", exc_info=True)
            return
        if leftovers:
            logger.warning(
                "The [identity] TOML lists nobody, but %d identity/identities from an earlier config are still in "
                "the database (%s), so unknown senders are guests. Keep them, or remove them with "
                "`pincer identity remove-channel` / PINCER_IDENTITY_MANAGEMENT=sync",
                len(leftovers),
                ", ".join(leftovers[:10]) + (", …" if len(leftovers) > 10 else ""),
            )

    async def _listed_identities(self, entries: list[tuple[str, list[_Pair], list[tuple[str, str]]]]) -> set[str]:
        """The identities the TOML lists: each entry's keeper (see `_entry_owners`)."""
        rename_targets = await self._rename_targets([(name, resolved) for name, resolved, _ in entries])
        listed: set[str] = set()
        for name, resolved, _unresolved in entries:
            _owned, keeper = await self._entry_owners(name, resolved, {}, rename_targets)
            listed.add(keeper)
        return listed

    async def cleanup_phones(self) -> None:
        """Remove the phones the config once listed and no longer does.

        `sync` drops every config-sourced number the TOML does not list;
        `update` only those of the people it lists; `clionly` none.

        A number that leaves the config must stop identifying its old owner on
        a call, whatever else fails at startup, so this never raises. The
        configured numbers are known without resolving any channel.
        """
        if self._mode == "clionly":
            return
        keep = {phone for entry in self._entries.values() for phone in entry.phones}
        try:
            owners: set[str] | None = None
            if self._mode != "sync":
                # The entries' names, and the identities they seed into: a
                # renamed entry keeps the old identity, whose phones these are.
                # Config ids as written: no channel is connected yet.
                owners = set(self._entries) | await self._listed_identities(await self._resolved_entries(None))
            removed = await self._store.prune_config_phones(keep, owners=owners)
        except Exception:
            logger.debug("Identity phone cleanup skipped: table unavailable", exc_info=True)
            return
        if removed:
            logger.info("Identity cleanup: removed %d phone(s) no longer in the config", removed)

    async def _unresolved_keep(
        self,
        entries: list[tuple[str, list[_Pair], list[tuple[str, str]]]],
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
            identity = {keeper}
            identity.update(uid for _pair, uid in owned if uid and uid.startswith("usr_"))
            identities.append(identity)
        configured = {name for name, _resolved, _unresolved in entries}.union(*identities)

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
        """Pre-create identity rows from the [identity] TOML on startup.

        Each entry's channel pairs share one pincer_user_id, its name.
        Passing ``channels`` causes each config ID to be translated to the
        channel's real internal ID (e.g. WhatsApp phone → LID, Telegram
        @username → numeric user_id) before being stored.

        In `sync` and `update` alike, the profile fields the TOML states
        overwrite the database and the ones it leaves out are kept; an
        existing identity keeps its `active` flag (see `IdentityService.seed`).
        In `clionly` there are no entries, so nothing happens.
        """
        if not self._entries:
            return

        entries: list[SeedEntry] = []
        # Every link is written in one transaction after the loop, so an entry
        # cannot find the pairs an earlier entry claimed in the database; it
        # finds them here instead.
        pending: dict[tuple[str, str], str] = {}

        async def rename(old_uid: str, new_uid: str) -> None:
            await self._rename_identity(old_uid, new_uid)
            for key, uid in pending.items():
                if uid == old_uid:
                    pending[key] = new_uid
            entries[:] = [
                dataclasses.replace(entry, pincer_user_id=new_uid) if entry.pincer_user_id == old_uid else entry
                for entry in entries
            ]

        resolved_entries = [(name, resolved) for name, resolved, _ in await self._resolved_entries(channels)]
        rename_targets = await self._rename_targets(resolved_entries)

        for name, resolved in resolved_entries:
            config = self._entries[name]
            resolved, keeper, existing_uids = await self._claim_pairs(name, resolved, pending, rename_targets)
            if not resolved:
                if config.phones:
                    logger.warning(
                        "Identity %s: no channel resolved, so its phone(s) are not stored yet — "
                        "calls from or to them count as guests until a restart where a channel resolves",
                        name,
                    )
                continue

            # Every existing identity but the keeper is unnamed (see
            # _claim_pairs) and is merged into it. A renamed entry seeds into
            # the old identity, configured under the new name.
            for uid in existing_uids:
                if uid != keeper:
                    logger.info("Identity conflict resolved: merging %s into %s", uid, keeper)
                    await rename(uid, keeper)

            config_fields: dict[str, Any] = {
                "display_name": config.display_name,
                "preferred_channel": config.preferred_channel,
                "email": config.email,
                "timezone": config.timezone,
            }
            entries.append(
                SeedEntry(
                    pincer_user_id=keeper,
                    channels=[(ch_str, norm) for ch_str, _ch_type, norm in resolved],
                    insert={**config_fields, "preferred_channel": config.preferred_channel or resolved[0][0]},
                    overwrite=config_fields,
                    phones=config.phones,
                )
            )
            for _ch_str, ch_type, norm in resolved:
                pending.setdefault((ch_type.value, norm), keeper)

        await self._store.seed(entries)
        logger.info("Identity config seeded (%s)", self._mode)

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
        multi-channel identity (linked via the [identity] TOML) the latter
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
        if not meta.get("active"):
            raise ValueError(f"Identity is deactivated: {pincer_user_id}")

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
    def _generate_user_id(channel: ChannelType, channel_user_id: str | int) -> str:
        raw = f"{channel.value}:{channel_user_id}"
        return f"usr_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
