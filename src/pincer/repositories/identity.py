"""Cross-channel identity: `identity_profiles` and `channel_identities`.

One profile per Pincer user; one `channel_identities` row per (channel,
channel user id) pair pointing at it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.identity import ChannelIdentity, IdentityProfile
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class IdentityProfileRepository(BaseRepository[IdentityProfile, str]):
    model = IdentityProfile

    async def add_if_new(self, values: dict[str, Any]) -> None:
        """Create the profile, leaving an existing one untouched."""
        await self.session.exec(
            upsert(dialect_of(self.session), IdentityProfile, values, index_elements=["pincer_user_id"])
        )

    async def backfill(self, pincer_user_id: str, values: dict[str, Any]) -> int:
        """Fill in the given fields where they are still empty.

        `COALESCE(new, stored)`: a field the caller left unset must not
        overwrite one the profile already has.
        """
        assignments = {name: func.coalesce(value, getattr(IdentityProfile, name)) for name, value in values.items()}
        stmt = (
            update(IdentityProfile).where(col(IdentityProfile.pincer_user_id) == pincer_user_id).values(**assignments)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def copy_to(self, old_id: str, new_id: str) -> None:
        """Give `new_id` a profile carrying `old_id`'s channel and name."""
        existing = await self.get(old_id)
        if existing is None:
            return
        await self.add_if_new(
            {
                "pincer_user_id": new_id,
                "preferred_channel": existing.preferred_channel,
                "display_name": existing.display_name,
            }
        )

    async def set_active_channel(self, pincer_user_id: str, channel: str, *, now: str) -> int:
        stmt = (
            update(IdentityProfile)
            .where(col(IdentityProfile.pincer_user_id) == pincer_user_id)
            .values(active_channel=channel, active_channel_updated_at=now)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def delete_by_id(self, pincer_user_id: str) -> int:
        return await self.delete_where(col(IdentityProfile.pincer_user_id) == pincer_user_id)

    async def search(self, term: str | None, limit: int) -> Sequence[IdentityProfile]:
        """Profiles, oldest first. `term` matches a Pincer user id or any of
        its channel ids."""
        stmt = select(IdentityProfile)
        if term:
            like = f"%{term}%"
            stmt = stmt.outerjoin(
                ChannelIdentity, col(ChannelIdentity.pincer_user_id) == col(IdentityProfile.pincer_user_id)
            ).where(col(IdentityProfile.pincer_user_id).like(like) | col(ChannelIdentity.channel_user_id).like(like))
        stmt = stmt.distinct().order_by(col(IdentityProfile.created_at)).limit(limit)
        return (await self.session.exec(stmt)).all()

    async def delete_unlinked(self) -> int:
        """Drop profiles no channel points at any more."""
        linked = select(col(ChannelIdentity.pincer_user_id))
        stmt = delete(IdentityProfile).where(col(IdentityProfile.pincer_user_id).notin_(linked))
        return int((await self.session.exec(stmt)).rowcount)


class ChannelIdentityRepository(BaseRepository[ChannelIdentity, tuple[str, str]]):
    model = ChannelIdentity

    async def user_for(self, channel: str, channel_user_id: str) -> str | None:
        stmt = select(ChannelIdentity.pincer_user_id).where(
            col(ChannelIdentity.channel) == channel, col(ChannelIdentity.channel_user_id) == channel_user_id
        )
        return (await self.session.exec(stmt)).first()

    async def link(self, channel: str, channel_user_id: str, pincer_user_id: str) -> None:
        """Point a channel id at a profile, leaving an existing link alone."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                ChannelIdentity,
                {"channel": channel, "channel_user_id": channel_user_id, "pincer_user_id": pincer_user_id},
                index_elements=["channel", "channel_user_id"],
            )
        )

    async def reassign(self, old_id: str, new_id: str) -> int:
        stmt = (
            update(ChannelIdentity).where(col(ChannelIdentity.pincer_user_id) == old_id).values(pincer_user_id=new_id)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def pairs(self) -> Sequence[tuple[str, str]]:
        """Every (channel, channel user id) pair, for the cleanup sweep."""
        stmt = select(ChannelIdentity.channel, ChannelIdentity.channel_user_id)
        return [(channel, cid) for channel, cid in (await self.session.exec(stmt)).all()]

    async def channels_for(self, pincer_user_id: str) -> Sequence[tuple[str, str]]:
        """This identity's channels, oldest link first."""
        stmt = (
            select(ChannelIdentity.channel, ChannelIdentity.channel_user_id)
            .where(col(ChannelIdentity.pincer_user_id) == pincer_user_id)
            .order_by(col(ChannelIdentity.created_at))
        )
        return [(channel, cid) for channel, cid in (await self.session.exec(stmt)).all()]

    async def links_for(self, pincer_user_id: str) -> Sequence[tuple[str, str, str | None]]:
        """This identity's channels with the time each was linked."""
        stmt = (
            select(ChannelIdentity.channel, ChannelIdentity.channel_user_id, ChannelIdentity.created_at)
            .where(col(ChannelIdentity.pincer_user_id) == pincer_user_id)
            .order_by(col(ChannelIdentity.created_at))
        )
        return [(channel, cid, linked_at) for channel, cid, linked_at in (await self.session.exec(stmt)).all()]

    async def unlink(self, channel: str, channel_user_id: str) -> int:
        return await self.delete_where(
            col(ChannelIdentity.channel) == channel, col(ChannelIdentity.channel_user_id) == channel_user_id
        )
