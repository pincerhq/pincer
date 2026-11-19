"""Cross-channel identity: `identity_profiles`, `identity_channels`,
`identity_phones`, `identity_credentials` and `auth_sessions`.

One profile per Pincer user; one `identity_channels` row per (channel,
channel user id) pair pointing at it; one `identity_phones` row per number;
at most one credentials row per profile.

The tables point at a profile by its `id` (`profile_id`, migration 0033).
Callers still name an identity by its `pincer_user_id` — the name the CLI,
the TOML, the API and a JWT use — and these methods translate: `profile_id_of`
for a write or a filter, a join on the profile for a read.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from sqlalchemy import Table, and_, delete, func, or_, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.db.metadata import metadata
from pincer.models.identity import (
    FOLLOWS,
    AuthSession,
    IdentityChannel,
    IdentityCredential,
    IdentityPhone,
    IdentityProfile,
    profile_id_of,
)
from pincer.models.scheduler import BriefingConfig
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def _profile_ids(pincer_user_ids: Iterable[str]) -> Any:
    return select(col(IdentityProfile.id)).where(col(IdentityProfile.pincer_user_id).in_(list(pincer_user_ids)))


class IdentityProfileRepository(BaseRepository[IdentityProfile, str]):
    model = IdentityProfile

    async def named(self, pincer_user_id: str) -> IdentityProfile | None:
        """The profile known by this name, or None."""
        return await self.get_by(pincer_user_id=pincer_user_id)

    async def add_if_new(self, values: dict[str, Any]) -> None:
        """Create the profile, leaving an existing one untouched."""
        await self.session.exec(
            upsert(dialect_of(self.session), IdentityProfile, values, index_elements=["pincer_user_id"])
        )

    async def overwrite(self, pincer_user_id: str, values: dict[str, Any]) -> int:
        """Set the given fields, replacing what is stored. A None value is left
        out: the caller did not state that field, so it keeps what it has."""
        assignments = {name: value for name, value in values.items() if value is not None}
        if not assignments:
            return 0
        stmt = (
            update(IdentityProfile).where(col(IdentityProfile.pincer_user_id) == pincer_user_id).values(**assignments)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def rename(self, old_id: str, new_id: str) -> int:
        """Give the profile a new name. Everything pointing at it follows: it points at the `id`."""
        stmt = (
            update(IdentityProfile).where(col(IdentityProfile.pincer_user_id) == old_id).values(pincer_user_id=new_id)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def set_active(self, pincer_user_id: str, active: bool) -> int:
        stmt = (
            update(IdentityProfile).where(col(IdentityProfile.pincer_user_id) == pincer_user_id).values(active=active)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def has_roster(self) -> bool:
        """True when an identity that was configured or made by hand has a
        channel, active or not.

        A deactivated one counts: switching the last person off must make
        them a guest, not open the gate for everyone. `auto` identities do
        not count: they are the unknown senders an ungated deployment lets
        in, and counting them would close the gate on everyone after the
        first message."""
        stmt = (
            select(IdentityProfile.pincer_user_id)
            .join(IdentityChannel, col(IdentityChannel.profile_id) == col(IdentityProfile.id))
            .where(col(IdentityProfile.source) != "auto")
            .limit(1)
        )
        return (await self.session.exec(stmt)).first() is not None

    async def ids_by_source(self, source: str, *, linked_only: bool = False) -> Sequence[str]:
        stmt = select(IdentityProfile.pincer_user_id).where(col(IdentityProfile.source) == source)
        if linked_only:
            stmt = stmt.where(col(IdentityProfile.id).in_(select(col(IdentityChannel.profile_id))))
        return (await self.session.exec(stmt.order_by(col(IdentityProfile.pincer_user_id)))).all()

    async def set_active_channel(self, pincer_user_id: str, channel: str, *, now: str) -> int:
        stmt = (
            update(IdentityProfile)
            .where(col(IdentityProfile.pincer_user_id) == pincer_user_id)
            .values(active_channel=channel, active_channel_updated_at=now)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def delete_by_id(self, pincer_user_id: str) -> int:
        """Delete the profile known by this name."""
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
            stmt = stmt.outerjoin(IdentityChannel, col(IdentityChannel.profile_id) == col(IdentityProfile.id)).where(
                col(IdentityProfile.pincer_user_id).like(like) | col(IdentityChannel.channel_user_id).like(like)
            )
        stmt = stmt.distinct().order_by(col(IdentityProfile.created_at)).limit(limit)
        return (await self.session.exec(stmt)).all()

    async def delete_unlinked(self, sources: Iterable[str]) -> int:
        """Drop profiles of the given sources that no channel points at any more.

        Except the ones that can sign in: an identity made for the dashboard
        or for an API key has no channel by design, and pruning it would lock
        its owner out at every restart.
        """
        linked = select(col(IdentityChannel.profile_id))
        stmt = delete(IdentityProfile).where(
            col(IdentityProfile.source).in_(list(sources)),
            col(IdentityProfile.id).notin_(linked),
            col(IdentityProfile.id).notin_(_can_sign_in()),
        )
        return int((await self.session.exec(stmt)).rowcount)


class IdentityChannelRepository(BaseRepository[IdentityChannel, str]):
    model = IdentityChannel

    async def user_for(self, channel: str, channel_user_id: str, *, active_only: bool = False) -> str | None:
        """The identity this channel id is linked to. With `active_only`, a
        deactivated owner counts as none."""
        stmt = (
            select(IdentityProfile.pincer_user_id)
            .join(IdentityChannel, col(IdentityChannel.profile_id) == col(IdentityProfile.id))
            .where(col(IdentityChannel.channel) == channel, col(IdentityChannel.channel_user_id) == channel_user_id)
        )
        if active_only:
            stmt = stmt.where(col(IdentityProfile.active).is_(True))
        return (await self.session.exec(stmt)).first()

    async def owner(self, channel: str, channel_user_id: str) -> tuple[str, bool] | None:
        """(owning identity, whether it is active) of this channel id, if linked."""
        stmt = (
            select(IdentityProfile.pincer_user_id, IdentityProfile.active)
            .join(IdentityChannel, col(IdentityChannel.profile_id) == col(IdentityProfile.id))
            .where(col(IdentityChannel.channel) == channel, col(IdentityChannel.channel_user_id) == channel_user_id)
        )
        row = (await self.session.exec(stmt)).first()
        return None if row is None else (row[0], bool(row[1]))

    async def link(self, channel: str, channel_user_id: str, pincer_user_id: str, *, alt_id: str | None = None) -> None:
        """Point a channel id at a profile, leaving an existing link alone.

        `alt_id` (the phone behind a WhatsApp LID) is recorded either way: it
        comes from the channel, which knows it better than the stored row.
        """
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                IdentityChannel,
                {"channel": channel, "channel_user_id": channel_user_id, "profile_id": profile_id_of(pincer_user_id)},
                index_elements=["channel", "channel_user_id"],
            )
        )
        if alt_id is not None:
            await self.remember_alt(channel, channel_user_id, alt_id)

    async def remember_alt(self, channel: str, channel_user_id: str, alt_id: str) -> int:
        """Record the channel's other id for a link; the number of links changed."""
        stmt = (
            update(IdentityChannel)
            .where(
                col(IdentityChannel.channel) == channel,
                col(IdentityChannel.channel_user_id) == channel_user_id,
                col(IdentityChannel.alt_id).is_distinct_from(alt_id),
            )
            .values(alt_id=alt_id)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def linked_as(self, channel: str, alt_id: str) -> str | None:
        """The channel id stored for this alternate id (the LID behind a phone), if any."""
        stmt = (
            select(IdentityChannel.channel_user_id)
            .where(col(IdentityChannel.channel) == channel, col(IdentityChannel.alt_id) == alt_id)
            .order_by(col(IdentityChannel.created_at).desc())
        )
        found: str | None = (await self.session.exec(stmt)).first()
        return found

    async def owner_by_alt(self, channel: str, alt_id: str) -> tuple[str, bool] | None:
        """(owning identity, whether it is active) of the link stored for this alternate id."""
        linked = await self.linked_as(channel, alt_id)
        return None if linked is None else await self.owner(channel, linked)

    async def unaltered_ids(self, pincer_user_id: str, channel: str) -> Sequence[str]:
        """This identity's ids on one channel that have no alternate id recorded."""
        stmt = select(IdentityChannel.channel_user_id).where(
            col(IdentityChannel.profile_id) == profile_id_of(pincer_user_id),
            col(IdentityChannel.channel) == channel,
            col(IdentityChannel.alt_id).is_(None),
        )
        return list((await self.session.exec(stmt)).all())

    async def reassign(self, old_id: str, new_id: str) -> int:
        stmt = (
            update(IdentityChannel)
            .where(col(IdentityChannel.profile_id) == profile_id_of(old_id))
            .values(profile_id=profile_id_of(new_id))
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def pairs(self) -> Sequence[tuple[str, str]]:
        """Every (channel, channel user id) pair."""
        stmt = select(IdentityChannel.channel, IdentityChannel.channel_user_id)
        return [(channel, cid) for channel, cid in (await self.session.exec(stmt)).all()]

    async def pairs_with_owner(self) -> Sequence[tuple[str, str, str | None, str | None]]:
        """Every (channel, channel user id, owner, owner's source), for the
        cleanup sweep. Owner and source are None for a link whose profile is gone."""
        stmt = select(
            IdentityChannel.channel,
            IdentityChannel.channel_user_id,
            IdentityProfile.pincer_user_id,
            IdentityProfile.source,
        ).outerjoin(IdentityProfile, col(IdentityProfile.id) == col(IdentityChannel.profile_id))
        return [(channel, cid, uid, source) for channel, cid, uid, source in (await self.session.exec(stmt)).all()]

    async def owners_on(self, channel: str) -> Sequence[tuple[str, str]]:
        """(channel user id, owning identity) of every link on one channel."""
        stmt = (
            select(IdentityChannel.channel_user_id, IdentityProfile.pincer_user_id)
            .join(IdentityProfile, col(IdentityProfile.id) == col(IdentityChannel.profile_id))
            .where(col(IdentityChannel.channel) == channel)
        )
        return [(cid, uid) for cid, uid in (await self.session.exec(stmt)).all()]

    async def channels_for(self, pincer_user_id: str) -> Sequence[tuple[str, str]]:
        """This identity's channels, oldest link first."""
        stmt = (
            select(IdentityChannel.channel, IdentityChannel.channel_user_id)
            .where(col(IdentityChannel.profile_id) == profile_id_of(pincer_user_id))
            .order_by(col(IdentityChannel.created_at))
        )
        return [(channel, cid) for channel, cid in (await self.session.exec(stmt)).all()]

    async def links_for(self, pincer_user_id: str) -> Sequence[tuple[str, str, str | None]]:
        """This identity's channels with the time each was linked."""
        stmt = (
            select(IdentityChannel.channel, IdentityChannel.channel_user_id, IdentityChannel.created_at)
            .where(col(IdentityChannel.profile_id) == profile_id_of(pincer_user_id))
            .order_by(col(IdentityChannel.created_at))
        )
        return [(channel, cid, linked_at) for channel, cid, linked_at in (await self.session.exec(stmt)).all()]

    async def unlink(self, channel: str, channel_user_id: str) -> int:
        return await self.delete_where(
            col(IdentityChannel.channel) == channel, col(IdentityChannel.channel_user_id) == channel_user_id
        )


class IdentityPhoneRepository(BaseRepository[IdentityPhone, str]):
    model = IdentityPhone

    def _owners(self) -> Any:
        return select(IdentityProfile.pincer_user_id).join(
            IdentityPhone, col(IdentityPhone.profile_id) == col(IdentityProfile.id)
        )

    async def owner_of_confirmed(self, phone: str) -> str | None:
        """The active identity whose confirmed number this is; None for an
        unknown or unconfirmed one, or one whose identity is deactivated."""
        stmt = self._owners().where(
            col(IdentityPhone.phone) == phone,
            col(IdentityPhone.confirmed).is_(True),
            col(IdentityProfile.active).is_(True),
        )
        found: str | None = (await self.session.exec(stmt)).first()
        return found

    async def confirmed_owners(self) -> Sequence[str]:
        """Active identities with at least one confirmed phone, however it was added."""
        stmt = (
            self._owners()
            .where(col(IdentityPhone.confirmed).is_(True), col(IdentityProfile.active).is_(True))
            .distinct()
            .order_by(col(IdentityProfile.pincer_user_id))
        )
        owners: Sequence[str] = (await self.session.exec(stmt)).all()
        return owners

    async def owner_of(self, phone: str) -> str | None:
        """Whoever holds this number, confirmed or not."""
        found: str | None = (await self.session.exec(self._owners().where(col(IdentityPhone.phone) == phone))).first()
        return found

    async def delete_for(self, phone: str, pincer_user_id: str) -> int:
        return await self.delete_where(
            col(IdentityPhone.phone) == phone, col(IdentityPhone.profile_id) == profile_id_of(pincer_user_id)
        )

    async def phones_for(self, pincer_user_id: str) -> Sequence[tuple[str, bool]]:
        """(phone, confirmed) of this identity, oldest first."""
        stmt = (
            select(IdentityPhone.phone, IdentityPhone.confirmed)
            .where(col(IdentityPhone.profile_id) == profile_id_of(pincer_user_id))
            .order_by(col(IdentityPhone.created_at), col(IdentityPhone.phone))
        )
        return [(phone, bool(confirmed)) for phone, confirmed in (await self.session.exec(stmt)).all()]

    async def phones_for_many(self, pincer_user_ids: Sequence[str]) -> dict[str, list[tuple[str, bool]]]:
        """`phones_for` of several identities in one query."""
        if not pincer_user_ids:
            return {}
        stmt = (
            select(IdentityProfile.pincer_user_id, IdentityPhone.phone, IdentityPhone.confirmed)
            .join(IdentityProfile, col(IdentityProfile.id) == col(IdentityPhone.profile_id))
            .where(col(IdentityProfile.pincer_user_id).in_(list(pincer_user_ids)))
            .order_by(col(IdentityPhone.created_at), col(IdentityPhone.phone))
        )
        phones: dict[str, list[tuple[str, bool]]] = {}
        for uid, phone, confirmed in (await self.session.exec(stmt)).all():
            phones.setdefault(uid, []).append((phone, bool(confirmed)))
        return phones

    async def upsert(self, phone: str, pincer_user_id: str, *, confirmed: bool, source: str, now: str) -> None:
        """Store the number for `pincer_user_id`; an existing row is taken over.

        A number that was confirmed already keeps the time it was first confirmed.
        """
        values = {"profile_id": profile_id_of(pincer_user_id), "confirmed": confirmed, "source": source}
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                IdentityPhone,
                {"phone": phone, "confirmed_at": now if confirmed else None, **values},
                index_elements=["phone"],
                # COALESCE with the row being inserted, not a bound `now`: a
                # bound string has no column type, and Postgres refuses to
                # coalesce TIMESTAMP with VARCHAR.
                set_=lambda excluded: {
                    **values,
                    "confirmed_at": (
                        func.coalesce(col(IdentityPhone.confirmed_at), excluded.confirmed_at) if confirmed else None
                    ),
                },
            )
        )

    async def reassign(self, old_id: str, new_id: str) -> int:
        stmt = (
            update(IdentityPhone)
            .where(col(IdentityPhone.profile_id) == profile_id_of(old_id))
            .values(profile_id=profile_id_of(new_id))
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def delete_config_phones_not_in(self, keep: Iterable[str], owners: Iterable[str] | None = None) -> int:
        """Drop the config-sourced numbers the config no longer lists; with
        `owners`, only those of these identities."""
        conditions = [col(IdentityPhone.source) == "config"]
        kept = list(keep)
        if kept:
            conditions.append(col(IdentityPhone.phone).notin_(kept))
        if owners is not None:
            scoped = list(owners)
            if not scoped:
                return 0
            conditions.append(col(IdentityPhone.profile_id).in_(_profile_ids(scoped)))
        return await self.delete_where(*conditions)

    async def delete_orphaned(self) -> int:
        """Drop numbers whose profile is gone (SQLite has no cascade)."""
        stmt = delete(IdentityPhone).where(col(IdentityPhone.profile_id).notin_(select(col(IdentityProfile.id))))
        return int((await self.session.exec(stmt)).rowcount)


def _has_credentials() -> Any:
    return col(IdentityCredential.password_hash).is_not(None) | col(IdentityCredential.api_key_hash).is_not(None)


def _can_sign_in() -> Any:
    """Profile ids of the identities that hold a password or an API key."""
    return select(col(IdentityCredential.profile_id)).where(_has_credentials())


def _first_token_version() -> int:
    """Where a new credentials row starts counting.

    Random rather than 0: a row can be deleted and made again under the same
    id, and a session token from the first life must not match the second.
    """
    return secrets.randbelow(2**30)


class IdentityCredentialRepository(BaseRepository[IdentityCredential, str]):
    model = IdentityCredential

    async def for_user(self, pincer_user_id: str) -> IdentityCredential | None:
        """The credentials of the identity known by this name, if it has any."""
        stmt = select(IdentityCredential).where(col(IdentityCredential.profile_id) == profile_id_of(pincer_user_id))
        return (await self.session.exec(stmt)).first()

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
                    "profile_id": profile_id_of(pincer_user_id),
                    "password_hash": password_hash,
                    "password_updated_at": now,
                    "token_version": _first_token_version(),
                    "created_at": now,
                },
                index_elements=["profile_id"],
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
            .where(col(IdentityCredential.profile_id) == profile_id_of(pincer_user_id))
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
                {
                    "profile_id": profile_id_of(pincer_user_id),
                    "token_version": _first_token_version(),
                    "created_at": now,
                    **key,
                },
                index_elements=["profile_id"],
                set_=key,
            )
        )

    async def move(self, old_id: str, new_id: str) -> None:
        """Hand `old_id`'s credentials to `new_id`.

        When both have a row this is a merge, and the target keeps its own:
        whoever signs in as `new_id` today must not find their password
        replaced by the identity that was folded into them.
        """
        if await self.for_user(new_id) is not None:
            await self.delete_by_id(old_id)
            return
        stmt = (
            update(IdentityCredential)
            .where(col(IdentityCredential.profile_id) == profile_id_of(old_id))
            .values(profile_id=profile_id_of(new_id))
        )
        await self.session.exec(stmt)

    async def delete_by_id(self, pincer_user_id: str) -> int:
        """Delete the credentials of the identity known by this name."""
        return await self.delete_where(col(IdentityCredential.profile_id) == profile_id_of(pincer_user_id))

    async def delete_unlinked_empty(self, sources: Iterable[str]) -> int:
        """Drop the rows of channel-less identities of the given sources that
        hold no credential, so the profile sweep that follows can take them."""
        linked = select(col(IdentityChannel.profile_id))
        of_sources = select(col(IdentityProfile.id)).where(col(IdentityProfile.source).in_(list(sources)))
        stmt = delete(IdentityCredential).where(
            col(IdentityCredential.profile_id).in_(of_sources),
            col(IdentityCredential.profile_id).notin_(linked),
            col(IdentityCredential.password_hash).is_(None),
            col(IdentityCredential.api_key_hash).is_(None),
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def count_usable(self) -> int:
        """Identities that can authenticate: a password or an API key."""
        stmt = select(func.count()).select_from(IdentityCredential).where(_has_credentials())
        return int((await self.session.exec(stmt)).one())


class AuthSessionRepository(BaseRepository[AuthSession, str]):
    model = AuthSession

    async def rotate(self, session_id: str, presented_jti: str, values: dict[str, Any]) -> int:
        """Swap the session's refresh token, but only while `presented_jti`
        is still the current one. 0 when another refresh got there first."""
        stmt = (
            update(AuthSession)
            .where(col(AuthSession.id) == session_id, col(AuthSession.refresh_jti) == presented_jti)
            .values(**values)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def reload(self, session_id: str) -> AuthSession | None:
        """The row as the database has it now, not as this unit of work
        first read it. None when it is gone."""
        stmt = select(AuthSession).where(col(AuthSession.id) == session_id).execution_options(populate_existing=True)
        return (await self.session.exec(stmt)).first()

    async def delete_by_id(self, session_id: str) -> int:
        return await self.delete_where(col(AuthSession.id) == session_id)

    async def delete_for_user(self, pincer_user_id: str) -> int:
        return await self.delete_where(col(AuthSession.profile_id) == profile_id_of(pincer_user_id))

    async def delete_expired(self, pincer_user_id: str, *, now: float) -> int:
        return await self.delete_where(
            col(AuthSession.profile_id) == profile_id_of(pincer_user_id), col(AuthSession.expires_at) < now
        )

    async def delete_orphaned(self) -> int:
        """Drop sessions whose credentials row is gone (SQLite has no cascade)."""
        owners = select(col(IdentityCredential.profile_id))
        stmt = delete(AuthSession).where(col(AuthSession.profile_id).notin_(owners))
        return int((await self.session.exec(stmt)).rowcount)


def profile_owned() -> tuple[tuple[Table, str], ...]:
    """The tables outside identity that name their user in text and keep a
    `profile_id` beside it (`profile_ref`), with that text column: the rows
    `follow_profile` moves."""
    return tuple(
        (table, column.info[FOLLOWS])
        for table in metadata.sorted_tables
        if (column := table.columns.get("profile_id")) is not None and FOLLOWS in column.info
    )


async def follow_profile(
    session: Any, old_name: str, new_name: str, *, profile_id: str | None, target_id: str | None
) -> None:
    """Move every row the identity `old_name` owns to `new_name`: its text and its `profile_id`.

    The same call for a rename (`target_id` is the profile's own id: only the
    text changes) and a merge (the rows move to the identity they were folded
    into). A row is the identity's by its `profile_id` or, where that is NULL
    (written before the profile existed), by its text: every reader looks rows
    up by the text, so both must move. A target that already has a briefing
    config keeps it, and the moving one goes: there is one per name.
    """

    def owned(table: Table, text_column: str) -> Any:
        by_text = table.c[text_column] == old_name
        return by_text if profile_id is None else or_(table.c.profile_id == profile_id, by_text)

    briefing = BriefingConfig.__table__  # type: ignore[attr-defined]
    # By the name, not the profile: the name is what is UNIQUE, and a config
    # can hold it with no `profile_id`. One already moving does not count.
    taken = briefing.c.pincer_user_id == new_name
    if profile_id is not None:
        taken = and_(taken, briefing.c.profile_id.is_distinct_from(profile_id))
    target_has = select(briefing.c.id).where(taken).exists()
    await session.exec(delete(briefing).where(owned(briefing, "pincer_user_id"), target_has))
    for table, text_column in profile_owned():
        await session.exec(
            update(table).where(owned(table, text_column)).values({"profile_id": target_id, text_column: new_name})
        )
