"""Identity storage: profiles, their channel links, and the moves between them.

`IdentityResolver` owns the rules — how a channel id becomes a Pincer user id,
what the configured identity map means, when a rename is allowed. This owns the
rows, and keeps each multi-table operation in one transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends

from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.repositories.identity import (
    AuthSessionRepository,
    ChannelIdentityRepository,
    IdentityCredentialRepository,
    IdentityPhoneRepository,
    IdentityProfileRepository,
)
from pincer.repositories.onboarding import OnboardingRepository
from pincer.repositories.sessions import SessionRepository
from pincer.services.base import DatabaseService
from pincer.services.onboarding import forget_user

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

from collections.abc import Sequence  # noqa: TC003 - dataclass field annotation

logger = logging.getLogger(__name__)

#: Where an identity came from: the [identity] TOML, `pincer identity …`, or
#: a `usr_` id made for an unknown sender.
SOURCE_CONFIG = "config"
SOURCE_CLI = "cli"
SOURCE_AUTO = "auto"

#: Sources a sync of the TOML may delete: what the TOML made, and the `usr_`
#: ids nobody made on purpose. Identities made by hand are never pruned.
PRUNABLE_SOURCES = (SOURCE_CONFIG, SOURCE_AUTO)


@dataclass(frozen=True)
class SeedEntry:
    """One identity from the configured map.

    `insert` is what a brand-new profile gets (its `preferred_channel` falls
    back to the entry's first channel). `overwrite` is only what the config
    states; it replaces what an existing identity has, and a field the config
    leaves out (None) is kept. `phones` (`+E.164`) are stored confirmed: the
    config is trusted.
    """

    pincer_user_id: str
    channels: Sequence[tuple[str, str]]
    insert: dict[str, Any]
    overwrite: dict[str, Any]
    phones: Sequence[str] = ()


class ChannelTakenError(Exception):
    """The channel id is linked to another identity already."""

    def __init__(self, channel: str, channel_user_id: str, owner: str) -> None:
        super().__init__(f"{channel}:{channel_user_id} already belongs to '{owner}'")
        self.channel, self.channel_user_id, self.owner = channel, channel_user_id, owner


class PhoneTakenError(Exception):
    """The number belongs to another identity already."""

    def __init__(self, phone: str, owner: str) -> None:
        super().__init__(f"{phone} already belongs to '{owner}'")
        self.phone, self.owner = phone, owner


@dataclass(frozen=True)
class NewApiKey:
    """What is stored of an API key: its hash and the two ends shown masked."""

    key_hash: str
    prefix: str
    last4: str


def _with_channels(
    profile: Any,
    links: Sequence[tuple[str, str, str | None]],
    phones: Sequence[tuple[str, bool]] = (),
) -> dict[str, Any]:
    return {
        "pincer_user_id": profile.pincer_user_id,
        "preferred_channel": profile.preferred_channel,
        "active_channel": profile.active_channel,
        "active_channel_updated_at": profile.active_channel_updated_at,
        "display_name": profile.display_name,
        "timezone": profile.timezone,
        "email": profile.email,
        "created_at": profile.created_at,
        "active": bool(profile.active),
        "source": profile.source,
        "channels": [
            {"channel": channel, "channel_user_id": channel_user_id, "linked_at": linked_at}
            for channel, channel_user_id, linked_at in links
        ],
        "phones": [{"phone": phone, "confirmed": confirmed} for phone, confirmed in phones],
    }


def _sql_now() -> str:
    """UTC in SQLite's `datetime('now')` format, which this column holds."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


class IdentityService(DatabaseService):
    # ── lookups ──────────────────────────────────────────────────────

    async def user_for_channel(self, channel: str, channel_user_id: str) -> str | None:
        """The active identity this channel id is linked to; a deactivated one counts as none."""
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).user_for(channel, channel_user_id, active_only=True)

    async def owner_any(self, channel: str, channel_user_id: str) -> tuple[str, bool] | None:
        """(owner, active) of this channel id whatever the owner's status, if linked."""
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).owner(channel, channel_user_id)

    async def has_roster(self) -> bool:
        """True when a configured or hand-made identity, active or not, has a channel (see the guest gate)."""
        async with session_scope(self._url) as session:
            return await IdentityProfileRepository(session).has_roster()

    async def ids_by_source(self, source: str, *, linked_only: bool = False) -> Sequence[str]:
        """Ids of the identities from `source`; with `linked_only`, only those with a channel."""
        async with session_scope(self._url) as session:
            return await IdentityProfileRepository(session).ids_by_source(source, linked_only=linked_only)

    async def user_for_confirmed_phone(self, phone: str) -> str | None:
        """The identity this number is a confirmed phone of, if any.

        `phone` is normalised first; a number that is not E.164 has no owner.
        """
        from pincer.core.phone import normalize_e164

        normalized = normalize_e164(phone)
        if normalized is None:
            return None
        async with session_scope(self._url) as session:
            return await IdentityPhoneRepository(session).owner_of_confirmed(normalized)

    async def profile(self, pincer_user_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await IdentityProfileRepository(session).get(pincer_user_id)
            return None if row is None else {name: getattr(row, name) for name in row.__class__.model_fields}

    async def channels_for(self, pincer_user_id: str) -> Sequence[tuple[str, str]]:
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).channels_for(pincer_user_id)

    async def owners_on(self, channel: str) -> Sequence[tuple[str, str]]:
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).owners_on(channel)

    async def list_profiles(self, *, search: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Identities with their linked channels, for the dashboard."""
        async with session_scope(self._url) as session:
            profiles = await IdentityProfileRepository(session).search(search, limit)
            links = ChannelIdentityRepository(session)
            phones = await IdentityPhoneRepository(session).phones_for_many([row.pincer_user_id for row in profiles])
            return [
                _with_channels(row, await links.links_for(row.pincer_user_id), phones.get(row.pincer_user_id, ()))
                for row in profiles
            ]

    async def profile_with_channels(self, pincer_user_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await IdentityProfileRepository(session).get(pincer_user_id)
            if row is None:
                return None
            links = await ChannelIdentityRepository(session).links_for(pincer_user_id)
            phones = await IdentityPhoneRepository(session).phones_for(pincer_user_id)
        return _with_channels(row, links, phones)

    # ── writes ───────────────────────────────────────────────────────

    async def create_identity(
        self,
        pincer_user_id: str,
        *,
        channel: str,
        channel_user_id: str,
        display_name: str | None = None,
        preferred_channel: str | None = None,
        source: str | None = None,
    ) -> None:
        """A new profile and its first channel link, together.

        `source` defaults to "auto" for a `usr_` id and "config" otherwise.
        """
        if source is None:
            source = SOURCE_AUTO if pincer_user_id.startswith("usr_") else SOURCE_CONFIG
        async with session_scope(self._url) as session:
            await IdentityProfileRepository(session).add_if_new(
                {
                    "pincer_user_id": pincer_user_id,
                    "preferred_channel": preferred_channel or channel,
                    "display_name": display_name,
                    "source": source,
                }
            )
            await ChannelIdentityRepository(session).link(channel, channel_user_id, pincer_user_id)

    async def create_profile(
        self,
        pincer_user_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        timezone: str | None = None,
        password_hash: str | None = None,
        api_key: NewApiKey | None = None,
        channels: Sequence[tuple[str, str]] = (),
        source: str = SOURCE_CLI,
    ) -> bool:
        """A profile made by hand (`pincer identity create`), with its
        channels and credential. False when the id is already taken; raises
        `ChannelTakenError` when a channel id belongs to someone else.

        All in one transaction, so a refused channel leaves no profile behind.
        """
        now = datetime.now(UTC).isoformat()
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            if await profiles.get(pincer_user_id) is not None:
                return False
            links = ChannelIdentityRepository(session)
            for channel, channel_user_id in channels:
                owner = await links.user_for(channel, channel_user_id)
                if owner is not None:
                    raise ChannelTakenError(channel, channel_user_id, owner)
            await profiles.add_if_new(
                {
                    "pincer_user_id": pincer_user_id,
                    "email": email,
                    "display_name": display_name,
                    "timezone": timezone,
                    "preferred_channel": channels[0][0] if channels else None,
                    "source": source,
                }
            )
            for channel, channel_user_id in channels:
                await links.link(channel, channel_user_id, pincer_user_id)
            credentials = IdentityCredentialRepository(session)
            if password_hash is not None:
                await credentials.set_password(pincer_user_id, password_hash, now=now)
            if api_key is not None:
                await credentials.set_api_key(
                    pincer_user_id, api_key_hash=api_key.key_hash, prefix=api_key.prefix, last4=api_key.last4, now=now
                )
        return True

    async def email_owners(self, email: str) -> list[str]:
        """Ids of the identities with this email (case-insensitive); at most two."""
        async with session_scope(self._url) as session:
            return [row.pincer_user_id for row in await IdentityProfileRepository(session).find_by_email_ci(email)]

    async def link_channel(self, pincer_user_id: str, channel: str, channel_user_id: str) -> None:
        async with session_scope(self._url) as session:
            await ChannelIdentityRepository(session).link(channel, channel_user_id, pincer_user_id)

    async def add_channel(self, pincer_user_id: str, channel: str, channel_user_id: str) -> bool:
        """Link a channel id to an existing identity. False when it is already
        linked to it; raises `ChannelTakenError` when it belongs to another
        and `LookupError` when the identity does not exist."""
        async with session_scope(self._url) as session:
            if await IdentityProfileRepository(session).get(pincer_user_id) is None:
                raise LookupError(pincer_user_id)
            links = ChannelIdentityRepository(session)
            owner = await links.user_for(channel, channel_user_id)
            if owner == pincer_user_id:
                return False
            if owner is not None:
                raise ChannelTakenError(channel, channel_user_id, owner)
            await links.link(channel, channel_user_id, pincer_user_id)
        return True

    async def unlink_channel(self, pincer_user_id: str, channel: str, channel_user_id: str) -> bool:
        """Remove one channel id from this identity. False when it was not linked to it."""
        async with session_scope(self._url) as session:
            links = ChannelIdentityRepository(session)
            if await links.user_for(channel, channel_user_id) != pincer_user_id:
                return False
            await links.unlink(channel, channel_user_id)
        return True

    async def update_profile(self, pincer_user_id: str, values: dict[str, Any]) -> bool:
        """Set the given profile fields (None ones are left alone). False for an unknown identity."""
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            if await profiles.get(pincer_user_id) is None:
                return False
            await profiles.overwrite(pincer_user_id, values)
        return True

    async def update_identity(
        self,
        pincer_user_id: str,
        fields: dict[str, Any],
        *,
        add_phones: Sequence[str] = (),
        remove_phones: Sequence[str] = (),
    ) -> list[str]:
        """Set profile fields (None ones are left alone) and add or remove
        confirmed phones, all in one transaction: a refused phone changes
        nothing. Returns the phones to remove that the identity did not have.

        Raises `LookupError` for an unknown identity and `PhoneTakenError`
        when a phone to add belongs to another identity.
        """
        now = datetime.now(UTC).isoformat()
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            if await profiles.get(pincer_user_id) is None:
                raise LookupError(pincer_user_id)
            phones = IdentityPhoneRepository(session)
            for phone in add_phones:
                owner = await phones.owner_of(phone)
                if owner is not None and owner != pincer_user_id:
                    raise PhoneTakenError(phone, owner)
            await profiles.overwrite(pincer_user_id, fields)
            for phone in add_phones:
                await phones.upsert(phone, pincer_user_id, confirmed=True, source=SOURCE_CLI, now=now)
            return [phone for phone in remove_phones if not await phones.delete_for(phone, pincer_user_id)]

    async def set_active(self, pincer_user_id: str, active: bool) -> bool:
        """Switch an identity on or off. False for an unknown identity.

        Switching it off also ends its signed-in sessions, in the same
        transaction; its password and API key are kept for when it is back.
        """
        async with session_scope(self._url) as session:
            if not await IdentityProfileRepository(session).set_active(pincer_user_id, active):
                return False
            if not active:
                await AuthSessionRepository(session).delete_for_user(pincer_user_id)
        return True

    async def add_phone(self, pincer_user_id: str, phone: str, *, source: str = SOURCE_CLI) -> None:
        """Store a confirmed `+E.164` number for this identity. Raises
        `PhoneTakenError` when another identity holds it and `LookupError`
        when the identity does not exist."""
        now = datetime.now(UTC).isoformat()
        async with session_scope(self._url) as session:
            if await IdentityProfileRepository(session).get(pincer_user_id) is None:
                raise LookupError(pincer_user_id)
            phones = IdentityPhoneRepository(session)
            owner = await phones.owner_of(phone)
            if owner is not None and owner != pincer_user_id:
                raise PhoneTakenError(phone, owner)
            await phones.upsert(phone, pincer_user_id, confirmed=True, source=source, now=now)

    async def remove_phone(self, pincer_user_id: str, phone: str) -> bool:
        """Drop one of this identity's numbers. False when it has no such number."""
        async with session_scope(self._url) as session:
            return bool(await IdentityPhoneRepository(session).delete_for(phone, pincer_user_id))

    async def replace_channel_links(
        self, pincer_user_id: str, channel: str, new_id: str, old_ids: Iterable[str]
    ) -> str | None:
        """Re-key an identity on one channel: link `new_id`, drop `old_ids`.

        One transaction, so the identity is never left with neither id. If
        `new_id` turns out to belong to another identity by now, nothing
        changes and that owner is returned; None means the re-key happened.
        """
        async with session_scope(self._url) as session:
            links = ChannelIdentityRepository(session)
            owner = await links.user_for(channel, new_id)
            if owner is not None and owner != pincer_user_id:
                return owner
            await links.link(channel, new_id, pincer_user_id)
            for old_id in old_ids:
                if old_id != new_id:
                    await links.unlink(channel, old_id)
        return None

    async def rename_identity(self, old_id: str, new_id: str) -> None:
        """Move an identity to a new id: profile, links, phones, credentials, its sessions and onboarding.

        One transaction — a half-applied rename would leave channel links
        pointing at a profile that no longer exists.
        """
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            await profiles.copy_to(old_id, new_id)
            await ChannelIdentityRepository(session).reassign(old_id, new_id)
            await IdentityPhoneRepository(session).reassign(old_id, new_id)
            # Before the old profile goes: on Postgres its deletion cascades to
            # the credentials, and SQLite (foreign keys off) would strand them.
            credentials = IdentityCredentialRepository(session)
            # Sessions name the old id in their tokens, so they cannot follow.
            await AuthSessionRepository(session).delete_for_user(old_id)
            if await profiles.get(new_id) is not None:
                await credentials.move(old_id, new_id)
            else:
                await credentials.delete_by_id(old_id)
            await profiles.delete_by_id(old_id)

            sessions = SessionRepository(session)
            await sessions.reassign_user(old_id, new_id)
            await sessions.rewrite_session_ids(old_id, new_id)
            await OnboardingRepository(session).reassign_user(old_id, new_id)
        forget_user(self._url, old_id)
        forget_user(self._url, new_id)

    async def touch_active_channel(self, pincer_user_id: str, channel: str) -> None:
        async with session_scope(self._url) as session:
            await IdentityProfileRepository(session).set_active_channel(pincer_user_id, channel, now=_sql_now())

    async def seed(self, identities: Iterable[SeedEntry]) -> None:
        """Apply the configured identity map: one profile per entry, with its
        channels linked and the profile fields the config states written over
        what is stored. One transaction, so a failure part-way leaves no
        half-seeded identity.

        A new profile is active; an existing one keeps its `active` flag, so
        the config never switches a deactivated identity back on. Every
        seeded profile becomes `source="config"`, including one made with the
        CLI that the config now lists.
        """
        now = datetime.now(UTC).isoformat()
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            links = ChannelIdentityRepository(session)
            phones = IdentityPhoneRepository(session)
            for entry in identities:
                await profiles.add_if_new(
                    {"pincer_user_id": entry.pincer_user_id, **entry.insert, "source": SOURCE_CONFIG}
                )
                # A no-op on the row just inserted; on an existing one (e.g.
                # made by resolve() or the CLI) the config wins per field.
                await profiles.overwrite(entry.pincer_user_id, {**entry.overwrite, "source": SOURCE_CONFIG})
                for channel, channel_user_id in entry.channels:
                    await links.link(channel, channel_user_id, entry.pincer_user_id)
                for phone in entry.phones:
                    await phones.upsert(phone, entry.pincer_user_id, confirmed=True, source="config", now=now)

    async def prune(
        self,
        keep: Iterable[tuple[str, str]],
        *,
        listed: Iterable[str] | None = None,
        sources: Sequence[str] = PRUNABLE_SOURCES,
    ) -> tuple[int, int]:
        """Unlink channel pairs outside `keep`. Returns (links removed, profiles removed).

        Without `listed` (sync): a pair goes when its owner's source is one of
        `sources` (by default the config and auto `usr_` ids); then such
        profiles left with no channel are dropped too. Identities made with
        the CLI are not touched.

        With `listed` (update): only pairs owned by these identities go, and
        no profile is dropped.

        An identity that holds a password or an API key is kept even with no
        channel: it was made to sign in, not to chat. Taking its access away
        is `pincer identity revoke`, not a side effect of editing the map.
        """
        allowed = set(keep)
        scope = None if listed is None else set(listed)
        async with session_scope(self._url) as session:
            links = ChannelIdentityRepository(session)
            removed = 0
            for channel, channel_user_id, owner, source in await links.pairs_with_owner():
                if (channel, channel_user_id) in allowed:
                    continue
                if scope is None and source is not None and source not in sources:
                    continue
                if scope is not None and owner not in scope:
                    continue
                removed += await links.unlink(channel, channel_user_id)
            if scope is not None:
                return removed, 0
            # Children first: SQLite runs with foreign keys off, so nothing
            # cascades there, and Postgres wants the child rows gone anyway.
            await IdentityCredentialRepository(session).delete_unlinked_empty(sources)
            await AuthSessionRepository(session).delete_orphaned()
            orphaned = await IdentityProfileRepository(session).delete_unlinked(sources)
            # After the profiles: a phone does not keep its identity alive, and
            # on SQLite nothing cascades to the numbers of a deleted profile.
            await IdentityPhoneRepository(session).delete_orphaned()
        return removed, orphaned

    async def prune_config_phones(self, keep: Iterable[str], *, owners: Iterable[str] | None = None) -> int:
        """Drop the numbers the config once listed but no longer does; with
        `owners`, only those of these identities.

        Numbers stored any other way are left alone.
        """
        async with session_scope(self._url) as session:
            return await IdentityPhoneRepository(session).delete_config_phones_not_in(keep, owners)


def roster_summary(db_path: Path) -> tuple[bool, list[str]] | None:
    """(whether the database holds a roster that gates guests, ids of CLI-made
    identities), for `pincer doctor`. None when there is no database yet."""
    from pincer.services.base import query_sync

    async def read(session: Any) -> tuple[bool, list[str]]:
        profiles = IdentityProfileRepository(session)
        return await profiles.has_roster(), sorted(await profiles.ids_by_source(SOURCE_CLI))

    return query_sync(db_path, read)


async def get_identity_service() -> IdentityService:
    """FastAPI dependency: identities on the configured database."""
    from pincer.config import get_settings_relaxed

    return IdentityService(get_database_url(get_settings_relaxed().db_path))


IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]
