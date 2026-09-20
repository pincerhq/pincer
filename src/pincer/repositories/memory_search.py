"""Full-text search over memories, per dialect.

The two engines have nothing in common but their job, so each is written in
its own SQL rather than pretended into one query builder:

* SQLite has the FTS5 virtual table `memories_fts`, kept in sync by triggers
  (migration 0001), matched with `MATCH` and ranked by `rank`.
* Postgres has a stored `tsvector` column with a GIN index (migration 0015),
  matched with `@@` and ranked by `ts_rank`.

Both return the same thing: memory ids, best match first, with a score where
larger is better.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel.ext.asyncio.session import AsyncSession


class MemorySearch(Protocol):
    async def ids_for(
        self, session: AsyncSession, query: str, *, user_id: str | None, limit: int
    ) -> Sequence[tuple[str, float]]:
        """(memory id, score) for `query`, best first."""
        ...


def terms(query: str) -> list[str]:
    return [word.strip() for word in query.split() if word.strip()]


class Fts5Search:
    """SQLite. A documented escape hatch: FTS5 has no SQLAlchemy construct."""

    async def ids_for(
        self, session: AsyncSession, query: str, *, user_id: str | None, limit: int
    ) -> Sequence[tuple[str, float]]:
        words = terms(query)
        if not words:
            return []
        # Quoted so punctuation in a word cannot be read as FTS5 syntax.
        match = " OR ".join(f'"{word}"' for word in words)
        sql = (
            "SELECT m.id, f.rank FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH :match"
        )
        params: dict[str, object] = {"match": match, "limit": limit}
        if user_id:
            sql += " AND m.user_id = :user_id"
            params["user_id"] = user_id
        sql += " ORDER BY f.rank LIMIT :limit"
        rows = (await session.exec(text(sql), params=params)).all()  # type: ignore[call-overload]
        # FTS5's rank is "more negative is better"; the caller wants larger-is-better.
        return [(str(row[0]), abs(float(row[1] or 0.0))) for row in rows]


class PostgresTextSearch:
    """Postgres, over the stored `search_vector` column from migration 0015."""

    async def ids_for(
        self, session: AsyncSession, query: str, *, user_id: str | None, limit: int
    ) -> Sequence[tuple[str, float]]:
        words = terms(query)
        if not words:
            return []
        match = " | ".join(words)  # OR, like the FTS5 side
        sql = (
            "SELECT id, ts_rank(search_vector, to_tsquery('simple', :match)) AS score "
            "FROM memories WHERE search_vector @@ to_tsquery('simple', :match)"
        )
        params: dict[str, object] = {"match": match, "limit": limit}
        if user_id:
            sql += " AND user_id = :user_id"
            params["user_id"] = user_id
        sql += " ORDER BY score DESC LIMIT :limit"
        rows = (await session.exec(text(sql), params=params)).all()  # type: ignore[call-overload]
        return [(str(row[0]), float(row[1] or 0.0)) for row in rows]


def search_for(dialect: str) -> MemorySearch:
    return Fts5Search() if dialect == "sqlite" else PostgresTextSearch()
