"""Agent conversation sessions, one row per user and channel."""

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, LargeBinary, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from pincer.db.ids import new_id
from pincer.db.types import Real, Uuid7
from pincer.models.identity import profile_ref

#: Session metadata marking a conversation that leaves long-term memory alone
#: (see `Agent.handle_message`'s `memory`): it does not make its user known.
MEMORY_FREE_KEY = "memory_free"


class ChatSession(SQLModel, table=True):
    """One conversation's history. `session_id` is its key, built from who and
    where (`unified:<user>`, `<channel>:<user>`, `web:<user>:<conversation>`,
    `voice:<call>`); `id` is the row's own."""

    __tablename__ = "pincer_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_sessions_session_id"),
        Index("idx_session_user", "user_id", "channel"),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    session_id: str = Field(sa_column=Column(Text(), nullable=False))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    # Moved on a rename by its own repository (`move_user`, `reassign_user`), not `follow_profile`.
    profile_id: str | None = profile_ref("user_id", follows=False)
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    messages: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'[]'")))
    session_metadata: str | None = Field(
        default=None, sa_column=Column("metadata", Text(), nullable=False, server_default=sa.text("'{}'"))
    )
    created_at: float = Field(sa_column=Column(Real(), nullable=False))
    updated_at: float = Field(sa_column=Column(Real(), nullable=False))


class SessionImage(SQLModel, table=True):
    """A photo kept in a conversation: written once, never updated, one per content hash and session.

    The messages hold `{sha256, media_type}` refs. The row points at the session's `id`, so a
    re-keyed session keeps its photos without touching them. Deleting the session cascades on
    Postgres; SQLite runs with foreign keys off (`db.engine`), so the code that deletes a session
    deletes its photos itself.
    """

    __tablename__ = "pincer_session_images"
    __table_args__ = (UniqueConstraint("session_ref", "sha256", name="uq_session_images_session_ref_sha256"),)

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    session_ref: str = Field(
        default=None,
        sa_column=Column(Uuid7(), ForeignKey("pincer_sessions.id", ondelete="CASCADE"), nullable=False),
    )
    sha256: str = Field(sa_column=Column(Text(), nullable=False))
    media_type: str = Field(sa_column=Column(Text(), nullable=False))
    data: bytes = Field(sa_column=Column(LargeBinary(), nullable=False))
    created_at: float = Field(sa_column=Column(Real(), nullable=False))
