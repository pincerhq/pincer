"""Cross-channel identity: one profile per Pincer user and the channel ids linked to it."""

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import IsoText


class IdentityProfile(SQLModel, table=True):
    __tablename__ = "identity_profiles"

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
    __tablename__ = "channel_identities"
    __table_args__ = (Index("idx_ci_pincer", "pincer_user_id"),)

    channel: str = Field(sa_column=Column(Text(), primary_key=True))
    channel_user_id: str = Field(sa_column=Column(Text(), primary_key=True))
    pincer_user_id: str = Field(
        sa_column=Column(Text(), ForeignKey("identity_profiles.pincer_user_id"), nullable=False)
    )
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
