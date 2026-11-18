"""Cross-channel identity: one profile per Pincer user, the channel ids linked
to it, its phone numbers, and the credentials it signs in to the API with."""

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, Text, UniqueConstraint, event, select
from sqlalchemy.orm import Mapper
from sqlmodel import Field, Relationship, SQLModel, col

from pincer.db.ids import new_id
from pincer.db.types import IsoText, Real, Uuid7


class IdentityProfile(SQLModel, table=True):
    """One Pincer user.

    `id` is the row's own key, and what every other table points at.
    `pincer_user_id` is the name the user is known by — a configured name or
    a `usr_` id — on the CLI, in the TOML, in a JWT's `sub` and in the API;
    renaming an identity changes only that column.
    """

    __tablename__ = "pincer_identity_profiles"
    __table_args__ = (UniqueConstraint("pincer_user_id", name="uq_identity_profiles_pincer_user_id"),)

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    pincer_user_id: str = Field(sa_column=Column(Text(), nullable=False))
    preferred_channel: str | None = Field(default=None, sa_column=Column(Text()))
    display_name: str | None = Field(default=None, sa_column=Column(Text()))
    active_channel: str | None = Field(default=None, sa_column=Column(Text()))
    active_channel_updated_at: str | None = Field(default=None, sa_column=Column(Text()))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
    email: str | None = Field(default=None, sa_column=Column(Text()))
    timezone: str | None = Field(default=None, sa_column=Column(Text()))
    # False: the identity is switched off (`pincer identity deactivate`). Its
    # channels and phones no longer identify it, and it cannot sign in.
    active: bool = Field(default=True, sa_column=Column(Boolean(), nullable=False, server_default=sa.text("TRUE")))
    # Where it came from: "config" (the [identity] TOML), "cli" (`pincer
    # identity …`) or "auto" (a `usr_` id made for an unknown sender).
    source: str = Field(default="config", sa_column=Column(Text(), nullable=False, server_default="config"))


class IdentityChannel(SQLModel, table=True):
    """A channel id (a Telegram chat, a WhatsApp number) linked to an identity."""

    __tablename__ = "pincer_identity_channels"
    __table_args__ = (
        UniqueConstraint("channel", "channel_user_id", name="uq_identity_channels_channel_channel_user_id"),
        Index("idx_ci_pincer", "profile_id"),
        Index("idx_identity_channels_alt", "channel", "alt_id"),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    channel_user_id: str = Field(sa_column=Column(Text(), nullable=False))
    profile_id: str = Field(sa_column=Column(Uuid7(), ForeignKey("pincer_identity_profiles.id"), nullable=False))
    #: The channel's other id for the same account: the phone number behind a
    #: WhatsApp LID. Lets a LID link stand for the configured phone when the
    #: channel cannot be asked, so it is never swapped back to the phone.
    alt_id: str | None = Field(default=None, sa_column=Column(Text(), nullable=True))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )


class IdentityCredential(SQLModel, table=True):
    """What an identity authenticates to the REST API with.

    A table of its own rather than columns on the profile: the profile is
    reflected field by field into `IdentityService.profile()` and copied per
    column on rename, so a hash stored there would either leak or be lost.

    Only hashes are kept. `api_key_prefix` and `api_key_last4` are the few
    characters of the key shown in its masked form; `token_version` is bumped
    on every password change, which invalidates the JWTs issued before it. A
    new row starts it at a random value, not 0, so a token that outlived a
    deleted row does not match the row that replaces it.
    """

    __tablename__ = "pincer_identity_credentials"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_identity_credentials_profile_id"),
        Index("idx_identity_credentials_api_key", "api_key_hash", unique=True),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    profile_id: str = Field(
        sa_column=Column(Uuid7(), ForeignKey("pincer_identity_profiles.id", ondelete="CASCADE"), nullable=False)
    )
    password_hash: str | None = Field(default=None, sa_column=Column(Text()))
    password_updated_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    api_key_hash: str | None = Field(default=None, sa_column=Column(Text()))
    api_key_prefix: str | None = Field(default=None, sa_column=Column(Text()))
    api_key_last4: str | None = Field(default=None, sa_column=Column(Text()))
    api_key_created_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    token_version: int = Field(default=0, sa_column=Column(Integer(), nullable=False, server_default=sa.text("0")))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )

    # Loaded with the row, so `pincer_user_id` reads after the session is gone.
    profile: IdentityProfile = Relationship(sa_relationship_kwargs={"lazy": "joined", "viewonly": True})

    @property
    def pincer_user_id(self) -> str:
        return self.profile.pincer_user_id


class IdentityPhone(SQLModel, table=True):
    """A phone number that belongs to an identity.

    A table of its own for the same reason as `IdentityCredential`: the
    profile is reflected field by field. The number is the key, so one number
    has one owner. Only a `confirmed` number tells a voice call that the
    person on the line is this identity; numbers from the config are
    confirmed, anything else starts unconfirmed.
    """

    __tablename__ = "pincer_identity_phones"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_identity_phones_phone"),
        Index("idx_identity_phones_user", "profile_id"),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    phone: str = Field(sa_column=Column(Text(), nullable=False))  # +E.164
    profile_id: str = Field(
        sa_column=Column(Uuid7(), ForeignKey("pincer_identity_profiles.id", ondelete="CASCADE"), nullable=False)
    )
    confirmed: bool = Field(default=False, sa_column=Column(Boolean(), nullable=False, server_default=sa.text("FALSE")))
    confirmed_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    source: str = Field(default="api", sa_column=Column(Text(), nullable=False))  # "config" | "api"
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )


class AuthSession(SQLModel, table=True):
    """One signed-in dashboard session: what makes a refresh token single-use
    and a logout real.

    Access and refresh tokens carry this row's id (`sid`). The row remembers
    the id of the one refresh token that is currently good; refreshing swaps
    it, so a copy of an earlier refresh token is worthless — and presenting
    one ends the session. Deleting the row signs the session out at once.

    `prev_refresh_jti` stays good for a few seconds after a rotation
    (`rotated_at`): two browser tabs whose access tokens expire together both
    refresh with the same token, and the slower one is not an attacker. It
    does not rotate again — it is handed the token the faster tab just got,
    so both hold the current one.
    """

    __tablename__ = "pincer_auth_sessions"
    __table_args__ = (Index("idx_auth_sessions_user", "profile_id"),)

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    # Through the credentials row: deleting it (a revoke) signs every session out.
    profile_id: str = Field(
        sa_column=Column(
            Uuid7(), ForeignKey("pincer_identity_credentials.profile_id", ondelete="CASCADE"), nullable=False
        )
    )
    refresh_jti: str = Field(sa_column=Column(Text(), nullable=False))
    prev_refresh_jti: str | None = Field(default=None, sa_column=Column(Text()))
    rotated_at: float | None = Field(default=None, sa_column=Column(Real()))
    expires_at: float = Field(sa_column=Column(Real(), nullable=False))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )

    profile: IdentityProfile = Relationship(
        sa_relationship_kwargs={
            "lazy": "joined",
            "viewonly": True,
            "primaryjoin": "foreign(AuthSession.profile_id) == IdentityProfile.id",
        }
    )

    @property
    def pincer_user_id(self) -> str:
        return self.profile.pincer_user_id


def profile_id_of(pincer_user_id: Any) -> Any:
    """The profile's `id` as a scalar subquery: NULL for a name with no profile.

    `pincer_user_id` is a name or a column (`excluded.pincer_user_id`, for an
    upsert that sets the text and has to keep `profile_id` in step with it).
    """
    return (
        select(col(IdentityProfile.id)).where(col(IdentityProfile.pincer_user_id) == pincer_user_id).scalar_subquery()
    )


#: `Column.info` key of a column whose insert value is derived from the row's
#: other values: a callable given a getter for them, returning the value (or
#: None for NULL). `pincer.db.dialect.upsert` and the ORM flush both apply it.
DERIVE = "derive"


#: `Column.info` key naming the text column of a `profile_ref` whose rows
#: `pincer.repositories.identity.follow_profile` moves on a rename or merge.
FOLLOWS = "follows_profile"


def profile_ref(text_column: str, *, follows: bool = True) -> Any:
    """`profile_id` for a table that names its user in `text_column`: nullable,
    indexed, set NULL when the profile goes.

    An insert fills it from the text: the profile of that name, or NULL for a
    sender with no profile (a `telegram:1` stand-in, a web visitor, `system`).
    As a subquery inside the INSERT, not a lookup before it.

    `follows=False` for a table whose own repository moves its rows on a
    rename, because they need more than the name changed.
    """

    def derive(get: Callable[[str], Any]) -> Any:
        name = get(text_column)
        return None if name is None else profile_id_of(name)

    return Field(
        default=None,
        sa_column=Column(
            "profile_id",
            Uuid7(),
            ForeignKey("pincer_identity_profiles.id", ondelete="SET NULL"),
            index=True,
            info={DERIVE: derive, **({FOLLOWS: text_column} if follows else {})},
        ),
    )


@event.listens_for(Mapper, "before_insert")
def _derive_on_flush(mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    """Fill the derived columns `add()`ed rows leave unset."""
    for column in mapper.local_table.columns:
        derive = column.info.get(DERIVE)
        if derive is not None and getattr(target, column.key, None) is None:
            value = derive(lambda name: getattr(target, name, None))
            if value is not None:
                setattr(target, column.key, value)
