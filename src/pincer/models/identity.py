"""Cross-channel identity: one profile per Pincer user, the channel ids linked
to it, and the credentials it signs in to the API with."""

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, Integer, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import IsoText


class IdentityProfile(SQLModel, table=True):
    __tablename__ = "pincer_identity_profiles"

    pincer_user_id: str = Field(sa_column=Column(Text(), primary_key=True))
    preferred_channel: str | None = Field(default=None, sa_column=Column(Text()))
    display_name: str | None = Field(default=None, sa_column=Column(Text()))
    active_channel: str | None = Field(default=None, sa_column=Column(Text()))
    active_channel_updated_at: str | None = Field(default=None, sa_column=Column(Text()))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
    email: str | None = Field(default=None, sa_column=Column(Text()))
    timezone: str | None = Field(default=None, sa_column=Column(Text()))


class ChannelIdentity(SQLModel, table=True):
    __tablename__ = "pincer_channel_identities"
    __table_args__ = (Index("idx_ci_pincer", "pincer_user_id"),)

    channel: str = Field(sa_column=Column(Text(), primary_key=True))
    channel_user_id: str = Field(sa_column=Column(Text(), primary_key=True))
    pincer_user_id: str = Field(
        sa_column=Column(Text(), ForeignKey("pincer_identity_profiles.pincer_user_id"), nullable=False)
    )
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
    on every password change, which invalidates the JWTs issued before it.
    """

    __tablename__ = "pincer_identity_credentials"
    __table_args__ = (Index("idx_identity_credentials_api_key", "api_key_hash", unique=True),)

    pincer_user_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("pincer_identity_profiles.pincer_user_id", ondelete="CASCADE"),
            primary_key=True,
        )
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
