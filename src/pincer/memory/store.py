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

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    user_id: str
    content: str
    category: str
    created_at: float
    score: float = 0.0


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


class MemoryStore:
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
                embedding_blob BLOB,
                created_at REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mem_user
            ON memories(user_id, category)
        """)

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

    async def store_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding: list[float] | None = None,
    ) -> str:
        """Store a memory entry. Returns the new memory ID."""
        assert self._db is not None
        mem_id = str(uuid.uuid4())
        blob = _pack_embedding(embedding) if embedding else None
        await self._db.execute(
            "INSERT INTO memories (id, user_id, content, category, embedding_blob, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mem_id, user_id, content, category, blob, time.time()),
        )
        await self._db.commit()
        logger.debug("Stored memory %s for user %s [%s]", mem_id[:8], user_id, category)
        return mem_id

    async def search_text(self, query: str, user_id: str | None = None, limit: int = 5) -> list[Memory]:
        """Full-text search over memories using FTS5."""
        assert self._db is not None
        # Build FTS5 query: split into words and OR them together for broad matching
        words = [w.strip() for w in query.split() if w.strip()]
        if not words:
            return []
        # Each word quoted to avoid FTS5 syntax issues, joined with OR
        fts_terms = " OR ".join(f'"{w}"' for w in words)

        if user_id:
            sql = """
                SELECT m.id, m.user_id, m.content, m.category, m.created_at,
                       rank
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ? AND m.user_id = ?
                ORDER BY rank
                LIMIT ?
            """
            params: tuple[str | int, ...] = (fts_terms, user_id, limit)
        else:
            sql = """
                SELECT m.id, m.user_id, m.content, m.category, m.created_at,
                       rank
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = (fts_terms, limit)

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
                        score=abs(float(row[5])) if row[5] else 0.0,
                    )
                )
        return results

    async def search_similar(self, embedding: list[float], user_id: str | None = None, limit: int = 5) -> list[Memory]:
        """Vector similarity search using cosine similarity on embeddings."""
        assert self._db is not None

        if user_id:
            sql = (
                "SELECT id, user_id, content, category, embedding_blob, created_at "
                "FROM memories WHERE user_id = ? AND embedding_blob IS NOT NULL"
            )
            params: tuple[str, ...] = (user_id,)
        else:
            sql = (
                "SELECT id, user_id, content, category, embedding_blob, created_at "
                "FROM memories WHERE embedding_blob IS NOT NULL"
            )
            params = ()

        scored: list[tuple[float, Memory]] = []
        async with self._db.execute(sql, params) as cursor:
            async for row in cursor:
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
                        ),
                    )
                )

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    async def get_recent_memories(self, user_id: str, limit: int = 10, category: str | None = None) -> list[Memory]:
        """Get most recent memories for a user."""
        assert self._db is not None
        if category:
            sql = (
                "SELECT id, user_id, content, category, created_at "
                "FROM memories WHERE user_id = ? AND category = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            sql_params: tuple[str | int, ...] = (user_id, category, limit)
        else:
            sql = (
                "SELECT id, user_id, content, category, created_at "
                "FROM memories WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            sql_params = (user_id, limit)

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
            ent_id = row[0]
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
