"""Agent conversation sessions, one row per user and channel."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, LargeBinary, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from pincer.db.ids import new_id
from pincer.db.types import JsonbDocument, JsonDocument, Real, UtcDateTime, Uuid7
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
        # The web chat's conversation list: a user's conversations, latest activity first
        Index("idx_sessions_user_channel_updated", "user_id", "channel", "updated_at", "id"),
        # Who owns a web chat conversation id: the session created first that names it
        Index(
            "idx_sessions_conversation_id",
            "conversation_id",
            "created_at",
            "id",
            postgresql_where=sa.text("conversation_id IS NOT NULL"),
            sqlite_where=sa.text("conversation_id IS NOT NULL"),
        ),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    session_id: str = Field(sa_column=Column(Text(), nullable=False))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    # Moved on a rename by its own repository (`move_user`, `reassign_user`), not `follow_profile`.
    profile_id: str | None = profile_ref("user_id", follows=False)
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    # Legacy: the history as one JSON list, read only by migration 0035, which moved it to
    # `pincer_session_messages`. Nothing writes it any more; a later migration drops it.
    messages: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'[]'")))
    session_metadata: str | None = Field(
        default=None, sa_column=Column("metadata", Text(), nullable=False, server_default=sa.text("'{}'"))
    )
    # The web chat conversation the session is (`web:<user>:<conversation_id>`), NULL for any other
    # session. The session created first that names an id owns it (`SessionRepository.owner_of`): another
    # identity naming it is refused, never given the owner's history. Not unique, so an identity rename
    # that joins two sessions of one person naming the same id (`_rekey_sessions`) never fails a save.
    # Kept on a re-key, which never changes the conversation.
    conversation_id: str | None = Field(default=None, sa_column=Column(Uuid7()))
    created_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    updated_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))


#: The roles a stored message can have (`llm.base.MessageRole`, which this layer does not import).
MESSAGE_ROLES = ("system", "user", "assistant", "tool_result")

#: The fields only some roles have, and those roles: NULL on every other role, which the table CHECKs
#: (`role_checks`) and a save holds to (`services.sessions`). One place, so the two cannot drift apart.
ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "tool_call_id": ("tool_result",),
    "tool_calls": ("assistant",),
    # A compaction window is a system message with the provider's output
    "raw": ("assistant", "system"),
    "wire": ("assistant", "system"),
    "context": ("user",),
    "channel_message_id": ("user",),
    "updated_at": ("user",),
}


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def role_checks(role_fields: dict[str, tuple[str, ...]] = ROLE_FIELDS) -> list[CheckConstraint]:
    """The CHECKs of `pincer_session_messages`: a known role, and each field of `role_fields` NULL
    outside its roles. Migration 0035 builds the same from its own frozen copy of the map."""
    return [
        CheckConstraint(f"role IN ({_quoted(MESSAGE_ROLES)})", name="ck_session_messages_role"),
        *(
            CheckConstraint(f"role IN ({_quoted(roles)}) OR {name} IS NULL", name=f"ck_session_messages_{name}")
            for name, roles in role_fields.items()
        ),
    ]


class SessionMessage(SQLModel, table=True):
    """One message of a conversation, in the order (`created_at`, `id`).

    A message that leaves the model's history (compaction, the emergency cut, a clear, a dropped tool
    pair) is not deleted: its row is *retired* (`retired_at`). The model is sent only the live rows; the
    history people read keeps them all. Only a message that moves to a new row loses its old one, since
    it lives on in the new row.

    `role` and `content` are always there; every other field belongs to some roles and is NULL
    elsewhere (never `''` or `[]`), as `ROLE_FIELDS` says. The CHECKs hold only what the code
    guarantees: a constraint the agent could break would fail a turn's save, not the bug.

    - `tool_call_id`: tool results only. `is_error`: an interrupted turn's result.
    - `tool_calls`: assistant only, `[{id, name, arguments}]`.
    - `raw` / `wire`: the provider's output verbatim and the "<provider>:<model>" that produced it,
      replayed while a request goes to the same wire (`llm.replay`). An assistant message's, or a
      compaction window's (a system message). `raw` and `tool_calls` are `json`, not `jsonb`, on
      Postgres: they come back with their keys in the order they were written, so a request rebuilt
      after a reload is the one sent before it.
    - `images`: `[{sha256, media_type}]`, the bytes in `pincer_session_images`.
    - `context`: the per-turn context of a user message (recalled memories, the time).
    - `channel_message_id`, `updated_at`: the messenger's id of a user message, and when it was
      edited there. User messages only.
    - `is_error` on an assistant message: a reply cut short (the client went away while it streamed);
      `content` is what was streamed of it.
    - `retired_at`: when the row left the model's history; NULL while it is in it.

    `id` is a UUIDv7 carrying `created_at`'s millisecond; within one millisecond it orders the rows.
    """

    __tablename__ = "pincer_session_messages"
    __table_args__ = (
        *role_checks(),
        Index("idx_session_messages_session_created", "session_id", "created_at", "id"),
        Index(
            "uq_session_messages_channel_message",
            "session_id",
            "channel_message_id",
            unique=True,
            postgresql_where=sa.text("channel_message_id IS NOT NULL"),
            sqlite_where=sa.text("channel_message_id IS NOT NULL"),
        ),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    session_id: str = Field(
        default=None,
        sa_column=Column(Uuid7(), ForeignKey("pincer_sessions.id", ondelete="CASCADE"), nullable=False),
    )
    role: str = Field(sa_column=Column(Text(), nullable=False))
    content: str = Field(default="", sa_column=Column(Text(), nullable=False, server_default=sa.text("''")))
    tool_call_id: str | None = Field(default=None, sa_column=Column(Text()))
    is_error: bool = Field(default=False, sa_column=Column(Boolean(), nullable=False, server_default=sa.false()))
    tool_calls: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JsonDocument))
    images: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JsonbDocument))
    context: str | None = Field(default=None, sa_column=Column(Text()))
    raw: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JsonDocument))
    wire: str | None = Field(default=None, sa_column=Column(Text()))
    channel_message_id: str | None = Field(default=None, sa_column=Column(Text()))
    created_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    updated_at: datetime | None = Field(default=None, sa_column=Column(UtcDateTime()))
    retired_at: datetime | None = Field(default=None, sa_column=Column(UtcDateTime()))


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
