"""Long-term memory: `memories`, `entities`, `conversations`.

Tags are a JSON array in a TEXT column, so a tag filter is a set of
`json_contains` predicates — ORed for "any of these tags", ANDed for "all of
them". That replaces a `json_each` join plus `GROUP BY … HAVING COUNT`, which
SQLite alone understands, and needs no `DISTINCT`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, json_contains
from pincer.db.ids import is_id
from pincer.models.memory import Conversation, Entity, Memory
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import ColumnElement


class MemoryRepository(BaseRepository[Memory, str]):
    model = Memory

    def _filters(
        self,
        *,
        user_id: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        match_all_tags: bool = False,
        with_embedding: bool = False,
    ) -> list[ColumnElement[bool]]:
        where: list[ColumnElement[bool]] = []
        if user_id is not None:
            where.append(col(Memory.user_id) == user_id)
        if category:
            where.append(col(Memory.category) == category)
        if with_embedding:
            where.append(col(Memory.embedding_blob).isnot(None))
        if tags:
            dialect = dialect_of(self.session)
            tags_column = Memory.__table__.c.tags  # type: ignore[attr-defined]
            matches = [json_contains(dialect, tags_column, tag) for tag in tags]
            where.append(and_(*matches) if match_all_tags else or_(*matches))
        return where

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
        newest_first: bool = True,
    ) -> Sequence[Memory]:
        order = col(Memory.created_at).desc() if newest_first else col(Memory.created_at)
        stmt = select(Memory).where(
            *self._filters(
                user_id=user_id,
                category=category,
                tags=tags,
                match_all_tags=match_all_tags,
                with_embedding=with_embedding,
            )
        )
        stmt = stmt.order_by(order)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        return (await self.session.exec(stmt)).all()

    async def by_ids(self, ids: Sequence[str]) -> Sequence[Memory]:
        """The rows for `ids`, in no particular order — the caller has the ranking."""
        if not ids:
            return []
        return (await self.session.exec(select(Memory).where(col(Memory.id).in_(list(ids))))).all()

    async def count(
        self,
        *,
        user_id: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        match_all_tags: bool = False,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Memory)
            .where(*self._filters(user_id=user_id, category=category, tags=tags, match_all_tags=match_all_tags))
        )
        return int((await self.session.exec(stmt)).one())

    async def distinct_users(self) -> int:
        stmt = select(func.count(func.distinct(col(Memory.user_id))))
        return int((await self.session.exec(stmt)).one())

    async def counts_by_category(self) -> dict[str, int]:
        stmt = select(Memory.category, func.count()).group_by(col(Memory.category))
        return {str(category): int(count) for category, count in (await self.session.exec(stmt)).all()}

    async def set_fields(self, memory_id: str, values: dict[str, Any]) -> int:
        """Updating an id that could never exist changes nothing, quietly —
        the same reasoning as `delete_by_id` and `BaseRepository.get`."""
        if not is_id(memory_id):
            return 0
        stmt = update(Memory).where(col(Memory.id) == memory_id).values(**values)
        return int((await self.session.exec(stmt)).rowcount)

    async def delete_by_id(self, memory_id: str) -> int:
        """Deleting an id that could never exist removes nothing, quietly —
        the same reasoning as `BaseRepository.get`."""
        if not is_id(memory_id):
            return 0
        return await self.delete_where(col(Memory.id) == memory_id)

    async def delete_for_user(self, user_id: str, *, category: str | None = None) -> int:
        where = [col(Memory.user_id) == user_id]
        if category:
            where.append(col(Memory.category) == category)
        return await self.delete_where(*where)


class EntityRepository(BaseRepository[Entity, str]):
    model = Entity

    async def find(self, user_id: str, name: str, type_: str) -> Entity | None:
        stmt = select(Entity).where(col(Entity.user_id) == user_id, col(Entity.name) == name, col(Entity.type) == type_)
        return (await self.session.exec(stmt)).first()

    async def touch(self, entity_id: str, *, attributes_json: str, last_seen: float) -> int:
        stmt = (
            update(Entity)
            .where(col(Entity.id) == entity_id)
            .values(attributes_json=attributes_json, last_seen=last_seen)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def for_user(self, user_id: str, *, type_: str | None = None, limit: int | None = None) -> Sequence[Entity]:
        where = [col(Entity.user_id) == user_id]
        if type_:
            where.append(col(Entity.type) == type_)
        return await self.list(*where, order_by=[col(Entity.last_seen).desc()], limit=limit)


class ConversationRepository(BaseRepository[Conversation, str]):
    model = Conversation
