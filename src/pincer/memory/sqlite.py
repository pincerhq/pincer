"""
The memory store on the configured database, with full-text search and optional
vector similarity. Full-text search is FTS5 on SQLite and a `tsvector` column on
Postgres (`pincer.repositories.memory_search`).

Tables:
- conversations: archived conversation snapshots
- memories: searchable memory entries (facts, summaries, etc.)
- entities: named entities extracted from conversations
"""

from __future__ import annotations

import json
import logging
import math
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pincer.db.ids import new_id
from pincer.memory.base import (
    PINCER_MEMORY_CATEGORY_TAG_PREFIX,
    PINCER_MEMORY_USER_TAG_PREFIX,
    BaseMemoryBackend,
    Memory,
)
from pincer.services.memory import MemoryService

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    user_id: str
    name: str
    type: str
    attributes: dict[str, str] = field(default_factory=dict)
    last_seen: float = 0.0


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _pack_embedding(embedding: list[float]) -> bytes:
    """Pack a list of floats into a compact bytes blob (float32)."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a bytes blob back into a list of floats."""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _memory_from_row(row: dict[str, Any], *, score: float = 0.0, tags: list[str] | None = None) -> Memory:
    return Memory(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        content=str(row["content"]),
        category=str(row["category"]),
        created_at=float(row["created_at"] or 0.0),
        tags=tags if tags is not None else (json.loads(row["tags"]) if row["tags"] else []),
        score=score,
    )


class SqlMemoryBackend(BaseMemoryBackend):
    """The memory store, with full-text and vector search, on SQLite or Postgres."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._service: MemoryService | None = None

    async def initialize(self) -> None:
        self._service = await MemoryService.for_path(self._db_path)
        logger.info("MemoryStore initialized at %s", self._db_path)

    async def close(self) -> None:
        # The engine is shared; the process disposes it at shutdown.
        self._service = None

    @property
    def _store(self) -> MemoryService:
        if self._service is None:
            raise RuntimeError("MemoryStore not initialized")
        return self._service

    # ── Memories ──────────────────────────────────────────

    async def get_memory(self, memory_id: str) -> Memory | None:
        row = await self._store.get(memory_id)
        return _memory_from_row(row) if row else None

    async def store_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding: list[float] | None = None,
        extra_tags: list[str] | None = None,
    ) -> str:
        """Store a memory entry. Returns the new memory ID."""
        mem_id = new_id()
        blob = _pack_embedding(embedding) if embedding else None

        tags = [f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}", f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}"]
        if extra_tags and isinstance(extra_tags, list):
            tags += extra_tags
        await self._store.add(
            {
                "id": mem_id,
                "user_id": user_id,
                "content": content,
                "category": category,
                "tags": json.dumps(tags),
                "embedding_blob": blob,
                "created_at": time.time(),
            }
        )
        logger.debug("Stored memory %s for user %s [%s]", mem_id[-8:], user_id, category)
        return mem_id

    async def add_profile(
        self,
        *,
        user_id: str,
        name: str | None,
        use_case: str | None,
        language: str | None,
    ) -> str | None:
        """Upsert a single 'profile' memory entry for a user.

        Replaces any prior profile entry so the latest information wins.
        Returns the new memory id, or None if no fields were provided.
        """
        parts: list[str] = []
        if name:
            parts.append(f"The user's name is {name}.")
        if use_case:
            parts.append(f"They mainly use Pincer for {use_case}.")
        if language:
            parts.append(f"Preferred language: {language}.")
        if not parts:
            return None

        await self._store.delete_for_user(user_id, category="profile")

        return await self.store_memory(
            user_id=user_id,
            content=" ".join(parts),
            category="profile",
        )

    async def search_text(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Full-text search over memories, with optional tag filtering (OR logic).

        The engine is whatever the database has: FTS5 on SQLite, a stored
        tsvector on Postgres (see `pincer.repositories.memory_search`).
        """
        rows = await self._store.full_text(query, user_id=user_id, limit=limit, tags=tags)
        return [_memory_from_row(row, score=float(row.get("score") or 0.0)) for row in rows]

    async def search_similar(
        self,
        embedding: list[float],
        user_id: str | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Vector similarity search using cosine similarity on embeddings, with optional tag filtering (OR logic)."""
        rows = await self._store.search(user_id=user_id, with_embedding=True, limit=None)

        tag_set = set(tags) if tags else None
        scored: list[tuple[float, Memory]] = []
        for row in rows:
            record_tags: list[str] = json.loads(row["tags"]) if row["tags"] else []
            if tag_set and not tag_set.intersection(record_tags):
                continue
            stored_emb = _unpack_embedding(row["embedding_blob"])
            score = _cosine_similarity(embedding, stored_emb)
            scored.append((score, _memory_from_row(row, score=score, tags=record_tags)))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [memory for _score, memory in scored[:limit]]

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update an existing memory in place. Only non-None fields are changed."""
        values: dict[str, Any] = {}
        if content is not None:
            values["content"] = content
        if category is not None:
            values["category"] = category
        if tags is not None:
            values["tags"] = json.dumps(tags)
        await self._store.set_fields(memory_id, values)

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a single memory by ID."""
        await self._store.delete(memory_id)

    async def count(
        self,
        user_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> int:
        """Return total number of memory records matching the given filters."""
        return await self._store.count(user_id=user_id, category=category, tags=tags, match_all_tags=match_all_tags)

    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memory records for a user. Returns the number of deleted rows."""
        deleted = await self._store.delete_for_user(user_id)
        logger.info("Deleted %d memories for user %s", deleted, user_id)
        return deleted

    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> list[Memory]:
        """List memories, newest first, with optional filtering and pagination."""
        rows = await self._store.search(
            user_id=user_id,
            category=category,
            tags=tags,
            match_all_tags=match_all_tags,
            limit=limit,
            offset=offset,
        )
        return [_memory_from_row(row) for row in rows]

    async def stats(self) -> tuple[int, dict[str, int]]:
        """(distinct users, memories per category), for `pincer memory stats`."""
        return await self._store.stats()

    # ── Entities ──────────────────────────────────────────

    async def store_entity(
        self,
        user_id: str,
        name: str,
        entity_type: str,
        attributes: dict[str, str] | None = None,
    ) -> str:
        """Store or update a named entity. Returns entity ID."""
        return await self._store.upsert_entity(
            {
                # A candidate, used only if this entity is new; the store
                # matches on (user, name, type) and returns the id that won.
                "id": new_id(),
                "user_id": user_id,
                "name": name,
                "type": entity_type,
                "attributes_json": json.dumps(attributes or {}),
                "last_seen": time.time(),
            }
        )

    async def get_entities(self, user_id: str, entity_type: str | None = None) -> list[Entity]:
        """Get all entities for a user, optionally filtered by type."""
        rows = await self._store.entities_for(user_id, type_=entity_type)
        return [
            Entity(
                id=str(row["id"]),
                user_id=str(row["user_id"]),
                name=str(row["name"]),
                type=str(row["type"]),
                attributes=json.loads(row["attributes_json"] or "{}"),
                last_seen=float(row["last_seen"] or 0.0),
            )
            for row in rows
        ]

    # ── Conversations ─────────────────────────────────────

    async def store_conversation(self, user_id: str, channel: str, messages_json: str) -> str:
        """Archive a conversation snapshot."""
        conv_id = new_id()
        now = time.time()
        await self._store.add_conversation(
            {
                "id": conv_id,
                "user_id": user_id,
                "channel": channel,
                "messages_json": messages_json,
                "created_at": now,
                "updated_at": now,
            }
        )
        return conv_id


#: The name this backend had when it was SQLite-only. The module keeps its name
#: for the same reason: importers of either still work.
SQLiteMemoryBackend = SqlMemoryBackend
