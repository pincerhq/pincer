"""
SQLite-backed memory store with FTS5 full-text search and optional vector similarity.

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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

from pincer.memory.base import (
    BaseMemoryBackend,
    Memory,
    PINCER_MEMORY_USER_TAG_PREFIX,
    PINCER_MEMORY_CATEGORY_TAG_PREFIX,
    PINCER_MEMORY_COUNT_FETCH_LIMIT
)

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


class SQLiteMemoryBackend(BaseMemoryBackend):
    """Async SQLite-backed memory store with full-text and vector search."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                messages_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_user
            ON conversations(user_id, channel)
        """)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                tags TEXT NOT NULL DEFAULT '[]',
                embedding_blob BLOB,
                created_at REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mem_user
            ON memories(user_id, category)
        """)

        # Migrate existing databases: add tags column if absent.
        async with self._db.execute("PRAGMA table_info(memories)") as cur:
            existing_columns = {row[1] async for row in cur}
        if "tags" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
            )
            logger.info("Migrated memories table: added tags column")

        # FTS5 index for full-text search on memories
        await self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, category,
                content=memories, content_rowid=rowid,
                tokenize='porter unicode61'
            )
        """)

        # Triggers to keep FTS in sync
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, category)
                VALUES (new.rowid, new.content, new.category);
            END
        """)
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category)
                VALUES ('delete', old.rowid, old.content, old.category);
            END
        """)
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category)
                VALUES ('delete', old.rowid, old.content, old.category);
                INSERT INTO memories_fts(rowid, content, category)
                VALUES (new.rowid, new.content, new.category);
            END
        """)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                last_seen REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ent_user
            ON entities(user_id, type)
        """)

        await self._db.commit()
        logger.info("MemoryStore initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Memories ──────────────────────────────────────────

    async def get_memory(self, memory_id: str) -> Memory | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, user_id, content, category, created_at, tags FROM memories WHERE id = ?",
            (memory_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return Memory(
            id=row[0],
            user_id=row[1],
            content=row[2],
            category=row[3],
            created_at=row[4],
            tags=json.loads(row[5]) if row[5] else [],
        )

    async def store_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding: list[float] | None = None,
        extra_tags: list[str] | None = None,
    ) -> str:
        """Store a memory entry. Returns the new memory ID."""
        assert self._db is not None
        mem_id = str(uuid.uuid4())
        blob = _pack_embedding(embedding) if embedding else None

        tags = [f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}", f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}"]
        if extra_tags and isinstance(extra_tags, list):
            tags += extra_tags
        await self._db.execute(
            "INSERT INTO memories (id, user_id, content, category, tags, embedding_blob, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mem_id, user_id, content, category, json.dumps(tags), blob, time.time()),
        )
        await self._db.commit()
        logger.debug("Stored memory %s for user %s [%s]", mem_id[:8], user_id, category)
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
        assert self._db is not None
        parts: list[str] = []
        if name:
            parts.append(f"The user's name is {name}.")
        if use_case:
            parts.append(f"They mainly use Pincer for {use_case}.")
        if language:
            parts.append(f"Preferred language: {language}.")
        if not parts:
            return None

        await self._db.execute(
            "DELETE FROM memories WHERE user_id = ? AND category = 'profile'",
            (user_id,),
        )
        await self._db.commit()

        return await self.store_memory(
            user_id=user_id,
            content=" ".join(parts),
            category="profile",
        )

    async def search_text(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = PINCER_MEMORY_COUNT_FETCH_LIMIT,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Full-text search over memories using FTS5, with optional tag filtering (OR logic)."""
        assert self._db is not None
        words = [w.strip() for w in query.split() if w.strip()]
        if not words:
            return []
        fts_terms = " OR ".join(f'"{w}"' for w in words)

        conditions = ["memories_fts MATCH ?"]
        params: list[object] = [fts_terms]

        if user_id:
            conditions.append("m.user_id = ?")
            params.append(user_id)

        tag_join = ""
        if tags:
            tag_join = ", json_each(m.tags) t"
            conditions.append(f"t.value IN ({','.join('?' * len(tags))})")
            params.extend(tags)

        where = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT DISTINCT m.id, m.user_id, m.content, m.category, m.created_at,
                   m.tags, rank
            FROM memories_fts f
            JOIN memories m ON m.rowid = f.rowid{tag_join}
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """

        results: list[Memory] = []
        async with self._db.execute(sql, params) as cursor:
            async for row in cursor:
                results.append(
                    Memory(
                        id=row[0],
                        user_id=row[1],
                        content=row[2],
                        category=row[3],
                        created_at=row[4],
                        tags=json.loads(row[5]) if row[5] else [],
                        score=abs(float(row[6])) if row[6] else 0.0,
                    )
                )
        return results

    async def search_similar(
        self,
        embedding: list[float],
        user_id: str | None = None,
        limit: int = PINCER_MEMORY_COUNT_FETCH_LIMIT,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Vector similarity search using cosine similarity on embeddings, with optional tag filtering (OR logic)."""
        assert self._db is not None

        if user_id:
            sql = (
                "SELECT id, user_id, content, category, embedding_blob, created_at, tags "
                "FROM memories WHERE user_id = ? AND embedding_blob IS NOT NULL"
            )
            sql_params: tuple[object, ...] = (user_id,)
        else:
            sql = (
                "SELECT id, user_id, content, category, embedding_blob, created_at, tags "
                "FROM memories WHERE embedding_blob IS NOT NULL"
            )
            sql_params = ()

        tag_set = set(tags) if tags else None

        scored: list[tuple[float, Memory]] = []
        async with self._db.execute(sql, sql_params) as cursor:
            async for row in cursor:
                record_tags: list[str] = json.loads(row[6]) if row[6] else []
                if tag_set and not tag_set.intersection(record_tags):
                    continue
                stored_emb = _unpack_embedding(row[4])
                score = _cosine_similarity(embedding, stored_emb)
                scored.append(
                    (
                        score,
                        Memory(
                            id=row[0],
                            user_id=row[1],
                            content=row[2],
                            category=row[3],
                            created_at=row[5],
                            score=score,
                            tags=record_tags,
                        ),
                    )
                )

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update an existing memory in place. Only non-None fields are changed."""
        assert self._db is not None
        updates: list[str] = []
        params: list[object] = []
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if category is not None:
            updates.append("category = ?")
            params.append(category)
        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags))
        if not updates:
            return
        params.append(memory_id)
        await self._db.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await self._db.commit()

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a single memory by ID."""
        assert self._db is not None
        await self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await self._db.commit()

    async def count(self, user_id: str | None = None) -> int:
        """Return total number of memory records, optionally scoped to a user."""
        assert self._db is not None
        if user_id:
            async with self._db.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        else:
            async with self._db.execute("SELECT COUNT(*) FROM memories") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memory records for a user. Returns the number of deleted rows."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        deleted = int(row[0]) if row else 0
        await self._db.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        await self._db.commit()
        logger.info("Deleted %d memories for user %s", deleted, user_id)
        return deleted

    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = PINCER_MEMORY_COUNT_FETCH_LIMIT,
        offset: int = 0,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> list[Memory]:
        """List memories, newest first, with optional filtering and pagination."""
        assert self._db is not None

        conditions: list[str] = []
        sql_params: list[object] = []

        if user_id is not None:
            conditions.append("m.user_id = ?")
            sql_params.append(user_id)

        if category:
            conditions.append("m.category = ?")
            sql_params.append(category)

        if tags and match_all_tags:
            # AND logic: every tag must appear — use GROUP BY + HAVING COUNT
            placeholders = ",".join("?" * len(tags))
            tag_join = ", json_each(m.tags) t"
            conditions.append(f"t.value IN ({placeholders})")
            sql_params.extend(tags)
            where_clause = f"WHERE {' AND '.join(conditions)} " if conditions else ""
            sql_params.extend([len(tags), limit, offset])
            sql = (
                f"SELECT m.id, m.user_id, m.content, m.category, m.created_at, m.tags "
                f"FROM memories m{tag_join} "
                f"{where_clause}"
                f"GROUP BY m.id "
                f"HAVING COUNT(DISTINCT t.value) = ? "
                f"ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
            )
        elif tags:
            # OR logic (default): any matching tag is sufficient
            placeholders = ",".join("?" * len(tags))
            tag_join = ", json_each(m.tags) t"
            conditions.append(f"t.value IN ({placeholders})")
            sql_params.extend(tags)
            where_clause = f"WHERE {' AND '.join(conditions)} " if conditions else ""
            sql_params.extend([limit, offset])
            sql = (
                f"SELECT DISTINCT m.id, m.user_id, m.content, m.category, m.created_at, m.tags "
                f"FROM memories m{tag_join} "
                f"{where_clause}"
                f"ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
            )
        else:
            where_clause = f"WHERE {' AND '.join(conditions)} " if conditions else ""
            sql_params.extend([limit, offset])
            sql = (
                f"SELECT m.id, m.user_id, m.content, m.category, m.created_at, m.tags "
                f"FROM memories m "
                f"{where_clause}"
                f"ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
            )

        results: list[Memory] = []
        async with self._db.execute(sql, sql_params) as cursor:
            async for row in cursor:
                results.append(
                    Memory(
                        id=row[0],
                        user_id=row[1],
                        content=row[2],
                        category=row[3],
                        created_at=row[4],
                        tags=json.loads(row[5]) if row[5] else [],
                    )
                )
        return results

    # ── Entities ──────────────────────────────────────────

    async def store_entity(
        self,
        user_id: str,
        name: str,
        entity_type: str,
        attributes: dict[str, str] | None = None,
    ) -> str:
        """Store or update a named entity. Returns entity ID."""
        assert self._db is not None
        now = time.time()
        attrs_json = json.dumps(attributes or {})

        # Upsert: update if same user+name+type exists
        async with self._db.execute(
            "SELECT id FROM entities WHERE user_id = ? AND name = ? AND type = ?",
            (user_id, name, entity_type),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            ent_id = str(row[0])
            await self._db.execute(
                "UPDATE entities SET attributes_json = ?, last_seen = ? WHERE id = ?",
                (attrs_json, now, ent_id),
            )
        else:
            ent_id = str(uuid.uuid4())
            await self._db.execute(
                "INSERT INTO entities (id, user_id, name, type, attributes_json, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (ent_id, user_id, name, entity_type, attrs_json, now),
            )

        await self._db.commit()
        return ent_id

    async def get_entities(self, user_id: str, entity_type: str | None = None) -> list[Entity]:
        """Get all entities for a user, optionally filtered by type."""
        assert self._db is not None
        if entity_type:
            sql = (
                "SELECT id, user_id, name, type, attributes_json, last_seen "
                "FROM entities WHERE user_id = ? AND type = ? ORDER BY last_seen DESC"
            )
            sql_params: tuple[str, ...] = (user_id, entity_type)
        else:
            sql = (
                "SELECT id, user_id, name, type, attributes_json, last_seen "
                "FROM entities WHERE user_id = ? ORDER BY last_seen DESC"
            )
            sql_params = (user_id,)

        results: list[Entity] = []
        async with self._db.execute(sql, sql_params) as cursor:
            async for row in cursor:
                results.append(
                    Entity(
                        id=row[0],
                        user_id=row[1],
                        name=row[2],
                        type=row[3],
                        attributes=json.loads(row[4]),
                        last_seen=row[5],
                    )
                )
        return results

    # ── Conversations ─────────────────────────────────────

    async def store_conversation(self, user_id: str, channel: str, messages_json: str) -> str:
        """Archive a conversation snapshot."""
        assert self._db is not None
        conv_id = str(uuid.uuid4())
        now = time.time()
        await self._db.execute(
            "INSERT INTO conversations "
            "(id, user_id, channel, messages_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, user_id, channel, messages_json, now, now),
        )
        await self._db.commit()
        return conv_id
