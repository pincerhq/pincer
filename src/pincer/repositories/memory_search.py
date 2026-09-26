"""Full-text search over memories, per dialect.

The two engines have nothing in common but their job, so each is written in
its own SQL rather than pretended into one query builder:

* SQLite has the FTS5 virtual table `memories_fts`, kept in sync by triggers
  (migration 0001), matched with `MATCH` and ranked by `rank`.
* Postgres has a stored `tsvector` column with a GIN index (migration 0015),
  matched with `@@` and ranked by `ts_rank`.

Both return the same thing: memory ids, best match first, with a score where
larger is better. Both take the whole filter — user, tags — because the row
that matches has to be found before the `LIMIT`, not after it.

Neither may let a user's words reach the query parser as syntax. A question
mark, an apostrophe or an exclamation mark is a search term to the person
typing it and an operator to both engines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from pincer.db.dialect import json_contains_sql

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel.ext.asyncio.session import AsyncSession


class MemorySearch(Protocol):
    async def ids_for(
        self,
        session: AsyncSession,
        query: str,
        *,
        user_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> Sequence[tuple[str, float]]:
        """(memory id, score) for `query`, best first."""
        ...


def terms(query: str) -> list[str]:
    return [word.strip() for word in query.split() if word.strip()]


def _tag_clause(dialect: str, column: str, tags: Sequence[str] | None) -> tuple[str, dict[str, object]]:
    """`AND (tag OR tag …)`, matching `MemoryRepository`'s any-of semantics."""
    if not tags:
        return "", {}
    predicates = [json_contains_sql(dialect, column, f"tag{i}") for i, _ in enumerate(tags)]
    return f" AND ({' OR '.join(predicates)})", {f"tag{i}": tag for i, tag in enumerate(tags)}


class Fts5Search:
    """SQLite. A documented escape hatch: FTS5 has no SQLAlchemy construct."""

    async def ids_for(
        self,
        session: AsyncSession,
        query: str,
        *,
        user_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> Sequence[tuple[str, float]]:
        words = terms(query)
        if not words:
            return []
        # Quoted so punctuation in a word cannot be read as FTS5 syntax, and
        # any quote of the user's own doubled, which is how FTS5 escapes one:
        # a bare `"` would otherwise end the string and raise "unterminated".
        match = " OR ".join('"{}"'.format(word.replace('"', '""')) for word in words)
        sql = (
            "SELECT m.id, f.rank FROM pincer_memories_fts f JOIN pincer_memories m ON m.rowid = f.rowid "
            "WHERE pincer_memories_fts MATCH :match"
        )
        params: dict[str, object] = {"match": match, "limit": limit}
        if user_id:
            sql += " AND m.user_id = :user_id"
            params["user_id"] = user_id
        clause, tag_params = _tag_clause("sqlite", "m.tags", tags)
        sql += clause
        params.update(tag_params)
        sql += " ORDER BY f.rank LIMIT :limit"
        rows = (await session.exec(text(sql), params=params)).all()  # type: ignore[call-overload]
        # FTS5's rank is "more negative is better"; the caller wants larger-is-better.
        return [(str(row[0]), abs(float(row[1] or 0.0))) for row in rows]


class PostgresTextSearch:
    """Postgres, over the stored `search_vector` column from migration 0015."""

    async def ids_for(
        self,
        session: AsyncSession,
        query: str,
        *,
        user_id: str | None,
        limit: int,
        tags: Sequence[str] | None = None,
    ) -> Sequence[tuple[str, float]]:
        words = terms(query)
        if not words:
            return []
        # `websearch_to_tsquery`, not `to_tsquery`: the latter parses its
        # argument as tsquery syntax, so an ordinary "Hey!" or "don't" is a
        # syntax error rather than a search. This one never raises. Each word
        # is quoted (with the user's own quotes dropped, since they would
        # rebalance the phrase) so `or` keeps its place as the operator and a
        # leading `-` stays a character instead of becoming a negation.
        match = " or ".join('"{}"'.format(word.replace('"', "")) for word in words)
        sql = (
            "SELECT id, ts_rank(search_vector, websearch_to_tsquery('simple', :match)) AS score "
            "FROM pincer_memories WHERE search_vector @@ websearch_to_tsquery('simple', :match)"
        )
        params: dict[str, object] = {"match": match, "limit": limit}
        if user_id:
            sql += " AND user_id = :user_id"
            params["user_id"] = user_id
        clause, tag_params = _tag_clause("postgresql", "tags", tags)
        sql += clause
        params.update(tag_params)
        sql += " ORDER BY score DESC LIMIT :limit"
        rows = (await session.exec(text(sql), params=params)).all()  # type: ignore[call-overload]
        return [(str(row[0]), float(row[1] or 0.0)) for row in rows]


def search_for(dialect: str) -> MemorySearch:
    return Fts5Search() if dialect == "sqlite" else PostgresTextSearch()
