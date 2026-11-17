"""Agent conversation sessions, one row per user and channel."""

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, LargeBinary, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import Real

#: Session metadata marking a conversation that leaves long-term memory alone
#: (see `Agent.handle_message`'s `memory`): it does not make its user known.
MEMORY_FREE_KEY = "memory_free"


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


class SessionImage(SQLModel, table=True):
    """A photo kept in a conversation: written once, never updated, keyed by its content hash.

    The messages hold `{sha256, media_type}` refs. Both foreign key actions cascade: Postgres
    removes a deleted session's photos and follows a re-keyed session (identity merge). SQLite
    runs with foreign keys off (`db.engine`), so the code that deletes or re-keys a session
    does the same to its photos itself.
    """

    __tablename__ = "pincer_session_images"

    session_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("pincer_sessions.session_id", ondelete="CASCADE", onupdate="CASCADE"),
            primary_key=True,
        )
    )
    sha256: str = Field(sa_column=Column(Text(), primary_key=True))
    media_type: str = Field(sa_column=Column(Text(), nullable=False))
    data: bytes = Field(sa_column=Column(LargeBinary(), nullable=False))
    created_at: float = Field(sa_column=Column(Real(), nullable=False))
