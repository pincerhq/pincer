"""Long-term memory storage.

`SQLiteMemoryBackend` owns the behaviour — embedding packing, cosine scoring,
what a profile memory means. This owns the rows, and picks the full-text
engine the database actually has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.dialect import dialect_of
from pincer.db.session import session_scope
from pincer.models.memory import Conversation, Entity, Memory
from pincer.repositories.memory import ConversationRepository, EntityRepository, MemoryRepository
from pincer.repositories.memory_search import search_for
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class MemoryService(DatabaseService):
    # ── memories ─────────────────────────────────────────────────────

    async def add(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await MemoryRepository(session).add(Memory(**values), refresh=False)

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await MemoryRepository(session).get(memory_id)
        return None if row is None else _as_dict(row)

    async def search(
        self,
        *,
        user_id: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        match_all_tags: bool = False,
        with_embedding: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await MemoryRepository(session).search(
                user_id=user_id,
                category=category,
                tags=tags,
                match_all_tags=match_all_tags,
                with_embedding=with_embedding,
                limit=limit,
                offset=offset,
            )
        return [_as_dict(row) for row in rows]

    async def full_text(
        self, query: str, *, user_id: str | None = None, limit: int = 20, tags: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Memories matching `query`, best match first, each with its `score`.

        The text engine returns ids and scores; the rows themselves (and any
        tag filter) come from the same place every other read does.
        """
        async with session_scope(self._url) as session:
            engine = search_for(dialect_of(session))
            ranked = await engine.ids_for(session, query, user_id=user_id, limit=limit)
            if not ranked:
                return []
            scores = dict(ranked)
            repo = MemoryRepository(session)
            rows = await repo.search(user_id=user_id, tags=tags, limit=None)
            found = [_as_dict(row) for row in rows if row.id in scores]

        for row in found:
            row["score"] = scores.get(str(row["id"]), 0.0)
        found.sort(key=lambda row: row["score"], reverse=True)
        return found[:limit]

    async def stats(self) -> tuple[int, dict[str, int]]:
        """(distinct users, memories per category) — what `pincer memory stats` shows."""
        async with session_scope(self._url) as session:
            repo = MemoryRepository(session)
            return await repo.distinct_users(), await repo.counts_by_category()

    async def set_fields(self, memory_id: str, values: dict[str, Any]) -> None:
        if not values:
            return
        async with session_scope(self._url) as session:
            await MemoryRepository(session).set_fields(memory_id, values)

    async def delete(self, memory_id: str) -> None:
        async with session_scope(self._url) as session:
            await MemoryRepository(session).delete_by_id(memory_id)

    async def count(
        self,
        *,
        user_id: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        match_all_tags: bool = False,
    ) -> int:
        async with session_scope(self._url) as session:
            return await MemoryRepository(session).count(
                user_id=user_id, category=category, tags=tags, match_all_tags=match_all_tags
            )

    async def delete_for_user(self, user_id: str, *, category: str | None = None) -> int:
        async with session_scope(self._url) as session:
            return await MemoryRepository(session).delete_for_user(user_id, category=category)

    # ── entities ─────────────────────────────────────────────────────

    async def upsert_entity(self, values: dict[str, Any]) -> None:
        """One row per (user, name, type); a later sighting updates it."""
        async with session_scope(self._url) as session:
            repo = EntityRepository(session)
            existing = await repo.find(str(values["user_id"]), str(values["name"]), str(values["type"]))
            if existing is None:
                await repo.add(Entity(**values), refresh=False)
                return
            await repo.touch(
                str(existing.id),
                attributes_json=str(values["attributes_json"]),
                last_seen=float(values["last_seen"]),
            )

    async def entities_for(self, user_id: str, *, type_: str | None = None, limit: int) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await EntityRepository(session).for_user(user_id, type_=type_, limit=limit)
        return [_as_dict(row) for row in rows]

    # ── conversations ────────────────────────────────────────────────

    async def add_conversation(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await ConversationRepository(session).add(Conversation(**values), refresh=False)


def _as_dict(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in row.__class__.model_fields}
