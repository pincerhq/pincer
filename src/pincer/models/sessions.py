"""Agent conversation sessions, one row per user and channel."""

import sqlalchemy as sa
from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import Real


class ChatSession(SQLModel, table=True):
    __tablename__ = "pincer_sessions"
    __table_args__ = (Index("idx_session_user", "user_id", "channel"),)

    session_id: str = Field(sa_column=Column(Text(), primary_key=True))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    messages: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'[]'")))
    session_metadata: str | None = Field(
        default=None, sa_column=Column("metadata", Text(), nullable=False, server_default=sa.text("'{}'"))
    )
    created_at: float = Field(sa_column=Column(Real(), nullable=False))
    updated_at: float = Field(sa_column=Column(Real(), nullable=False))
