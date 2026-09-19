"""Long-term memory: conversations, memories (plus the hand-written FTS5 index) and entities."""

import sqlalchemy as sa
from sqlalchemy import Column, Index, LargeBinary, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import Real


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"
    __table_args__ = (Index("idx_conv_user", "user_id", "channel"),)

    id: str = Field(sa_column=Column(Text(), primary_key=True))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    messages_json: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'[]'"))
    )
    created_at: float = Field(sa_column=Column(Real(), nullable=False))
    updated_at: float = Field(sa_column=Column(Real(), nullable=False))


class Memory(SQLModel, table=True):
    __tablename__ = "memories"
    __table_args__ = (Index("idx_mem_user", "user_id", "category"),)

    id: str = Field(sa_column=Column(Text(), primary_key=True))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    content: str = Field(sa_column=Column(Text(), nullable=False))
    category: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'general'"))
    )
    tags: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'[]'")))
    embedding_blob: bytes | None = Field(default=None, sa_column=Column(LargeBinary()))
    created_at: float = Field(sa_column=Column(Real(), nullable=False))


class Entity(SQLModel, table=True):
    __tablename__ = "entities"
    __table_args__ = (Index("idx_ent_user", "user_id", "type"),)

    id: str = Field(sa_column=Column(Text(), primary_key=True))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    name: str = Field(sa_column=Column(Text(), nullable=False))
    type: str = Field(sa_column=Column(Text(), nullable=False))
    attributes_json: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'{}'"))
    )
    last_seen: float = Field(sa_column=Column(Real(), nullable=False))
