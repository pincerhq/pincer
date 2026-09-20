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
from pincer.repositories.identity import ChannelIdentityRepository, IdentityProfileRepository
from pincer.repositories.sessions import SessionRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Iterable

from collections.abc import Sequence  # noqa: TC003 - dataclass field annotation

logger = logging.getLogger(__name__)

#: Profile fields a caller may backfill; `preferred_channel` is write-once,
#: and day-to-day channel use is tracked by `active_channel` instead.
BACKFILL_FIELDS = ("display_name", "preferred_channel", "email", "timezone")


@dataclass(frozen=True)
class SeedEntry:
    """One identity from the configured map.

    `insert` is what a brand-new profile gets (its `preferred_channel` falls
    back to the entry's first channel). `backfill` is only what the config
    states, so an identity that already exists keeps what it has.
    """

    pincer_user_id: str
    channels: Sequence[tuple[str, str]]
    insert: dict[str, Any]
    backfill: dict[str, Any]


def _with_channels(profile: Any, links: Sequence[tuple[str, str, str | None]]) -> dict[str, Any]:
    return {
        "pincer_user_id": profile.pincer_user_id,
        "preferred_channel": profile.preferred_channel,
        "active_channel": profile.active_channel,
        "active_channel_updated_at": profile.active_channel_updated_at,
        "display_name": profile.display_name,
        "timezone": profile.timezone,
        "email": profile.email,
        "created_at": profile.created_at,
        "channels": [
            {"channel": channel, "channel_user_id": channel_user_id, "linked_at": linked_at}
            for channel, channel_user_id, linked_at in links
        ],
    }


def _sql_now() -> str:
    """UTC in SQLite's `datetime('now')` format, which this column holds."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


class IdentityService(DatabaseService):
    # ── lookups ──────────────────────────────────────────────────────

    async def user_for_channel(self, channel: str, channel_user_id: str) -> str | None:
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).user_for(channel, channel_user_id)

    async def profile(self, pincer_user_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await IdentityProfileRepository(session).get(pincer_user_id)
            return None if row is None else {name: getattr(row, name) for name in row.__class__.model_fields}

    async def channels_for(self, pincer_user_id: str) -> Sequence[tuple[str, str]]:
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).channels_for(pincer_user_id)

    async def channel_pairs(self) -> Sequence[tuple[str, str]]:
        async with session_scope(self._url) as session:
            return await ChannelIdentityRepository(session).pairs()

    async def list_profiles(self, *, search: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Identities with their linked channels, for the dashboard."""
        async with session_scope(self._url) as session:
            profiles = await IdentityProfileRepository(session).search(search, limit)
            links = ChannelIdentityRepository(session)
            return [_with_channels(row, await links.links_for(row.pincer_user_id)) for row in profiles]

    async def profile_with_channels(self, pincer_user_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await IdentityProfileRepository(session).get(pincer_user_id)
            if row is None:
                return None
            links = await ChannelIdentityRepository(session).links_for(pincer_user_id)
        return _with_channels(row, links)

    # ── writes ───────────────────────────────────────────────────────

    async def create_identity(
        self,
        pincer_user_id: str,
        *,
        channel: str,
        channel_user_id: str,
        display_name: str | None = None,
        preferred_channel: str | None = None,
    ) -> None:
        """A new profile and its first channel link, together."""
        async with session_scope(self._url) as session:
            await IdentityProfileRepository(session).add_if_new(
                {
                    "pincer_user_id": pincer_user_id,
                    "preferred_channel": preferred_channel or channel,
                    "display_name": display_name,
                }
            )
            await ChannelIdentityRepository(session).link(channel, channel_user_id, pincer_user_id)

    async def link_channel(self, pincer_user_id: str, channel: str, channel_user_id: str) -> None:
        async with session_scope(self._url) as session:
            await ChannelIdentityRepository(session).link(channel, channel_user_id, pincer_user_id)

    async def rename_identity(self, old_id: str, new_id: str) -> None:
        """Move an identity to a new id: profile, links and its sessions.

        One transaction — a half-applied rename would leave channel links
        pointing at a profile that no longer exists.
        """
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            await profiles.copy_to(old_id, new_id)
            await ChannelIdentityRepository(session).reassign(old_id, new_id)
            await profiles.delete_by_id(old_id)

            sessions = SessionRepository(session)
            await sessions.reassign_user(old_id, new_id)
            await sessions.rewrite_session_ids(old_id, new_id)

    async def touch_active_channel(self, pincer_user_id: str, channel: str) -> None:
        async with session_scope(self._url) as session:
            await IdentityProfileRepository(session).set_active_channel(pincer_user_id, channel, now=_sql_now())

    async def seed(self, identities: Iterable[SeedEntry]) -> None:
        """Apply the configured identity map: one profile per entry, with its
        channels linked and its profile fields backfilled. One transaction, so
        a failure part-way leaves no half-seeded identity.
        """
        async with session_scope(self._url) as session:
            profiles = IdentityProfileRepository(session)
            links = ChannelIdentityRepository(session)
            for entry in identities:
                await profiles.add_if_new({"pincer_user_id": entry.pincer_user_id, **entry.insert})
                if any(value is not None for value in entry.backfill.values()):
                    # The insert above was a no-op for an identity that already
                    # existed (e.g. created by resolve()); this fills in what is
                    # still empty without clobbering what is not.
                    await profiles.backfill(entry.pincer_user_id, entry.backfill)
                for channel, channel_user_id in entry.channels:
                    await links.link(channel, channel_user_id, entry.pincer_user_id)

    async def prune(self, keep: Iterable[tuple[str, str]]) -> tuple[int, int]:
        """Unlink every channel pair outside `keep`, then drop profiles left
        with no channels. Returns (links removed, profiles removed)."""
        allowed = set(keep)
        async with session_scope(self._url) as session:
            links = ChannelIdentityRepository(session)
            removed = 0
            for pair in await links.pairs():
                if pair not in allowed:
                    removed += await links.unlink(*pair)
            orphaned = await IdentityProfileRepository(session).delete_unlinked()
        return removed, orphaned


async def get_identity_service() -> IdentityService:
    """FastAPI dependency: identities on the configured database."""
    from pincer.config import get_settings_relaxed

    return IdentityService(get_database_url(get_settings_relaxed().db_path))


IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]
