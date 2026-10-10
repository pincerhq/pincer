"""Cross-channel identity: `identity_profiles`, `channel_identities` and
`identity_credentials`.

One profile per Pincer user; one `channel_identities` row per (channel,
channel user id) pair pointing at it; at most one credentials row per profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.identity import ChannelIdentity, IdentityCredential, IdentityProfile
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

    async def find_by_email_ci(self, email: str) -> Sequence[IdentityProfile]:
        """Profiles whose email matches, ignoring case.

        At most two: the caller only needs to tell "exactly one" from "none"
        and "ambiguous", and an email is not unique across identities.
        """
        stmt = select(IdentityProfile).where(func.lower(col(IdentityProfile.email)) == email.strip().lower()).limit(2)
        return (await self.session.exec(stmt)).all()

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


class IdentityCredentialRepository(BaseRepository[IdentityCredential, str]):
    model = IdentityCredential

    async def by_api_key_hash(self, api_key_hash: str) -> IdentityCredential | None:
        stmt = select(IdentityCredential).where(col(IdentityCredential.api_key_hash) == api_key_hash)
        return (await self.session.exec(stmt)).first()

    async def set_password(self, pincer_user_id: str, password_hash: str, *, now: str) -> None:
        """Store a new password and invalidate the sessions issued under the old one."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                IdentityCredential,
                {
                    "pincer_user_id": pincer_user_id,
                    "password_hash": password_hash,
                    "password_updated_at": now,
                    "created_at": now,
                },
                index_elements=["pincer_user_id"],
                set_={
                    "password_hash": password_hash,
                    "password_updated_at": now,
                    "token_version": col(IdentityCredential.token_version) + 1,
                },
            )
        )

    async def replace_password_hash(self, pincer_user_id: str, password_hash: str) -> int:
        """Swap in a re-hash of the same password: sessions stay valid."""
        stmt = (
            update(IdentityCredential)
            .where(col(IdentityCredential.pincer_user_id) == pincer_user_id)
            .values(password_hash=password_hash)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def set_api_key(self, pincer_user_id: str, *, api_key_hash: str, prefix: str, last4: str, now: str) -> None:
        key = {
            "api_key_hash": api_key_hash,
            "api_key_prefix": prefix,
            "api_key_last4": last4,
            "api_key_created_at": now,
        }
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                IdentityCredential,
                {"pincer_user_id": pincer_user_id, "created_at": now, **key},
                index_elements=["pincer_user_id"],
                set_=key,
            )
        )

    async def move(self, old_id: str, new_id: str) -> None:
        """Hand `old_id`'s credentials to `new_id`.

        When both have a row this is a merge, and the target keeps its own:
        whoever signs in as `new_id` today must not find their password
        replaced by the identity that was folded into them.
        """
        if await self.get(new_id) is not None:
            await self.delete_by_id(old_id)
            return
        stmt = (
            update(IdentityCredential)
            .where(col(IdentityCredential.pincer_user_id) == old_id)
            .values(pincer_user_id=new_id)
        )
        await self.session.exec(stmt)

    async def delete_by_id(self, pincer_user_id: str) -> int:
        return await self.delete_where(col(IdentityCredential.pincer_user_id) == pincer_user_id)

    async def delete_unlinked(self) -> int:
        """Drop the credentials of identities no channel points at any more."""
        linked = select(col(ChannelIdentity.pincer_user_id))
        stmt = delete(IdentityCredential).where(col(IdentityCredential.pincer_user_id).notin_(linked))
        return int((await self.session.exec(stmt)).rowcount)

    async def count_usable(self) -> int:
        """Identities that can authenticate: a password or an API key."""
        stmt = (
            select(func.count())
            .select_from(IdentityCredential)
            .where(
                col(IdentityCredential.password_hash).is_not(None) | col(IdentityCredential.api_key_hash).is_not(None)
            )
        )
        return int((await self.session.exec(stmt)).one())
