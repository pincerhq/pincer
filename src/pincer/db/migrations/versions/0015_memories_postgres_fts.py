"""Give Postgres a full-text index over memories.

SQLite searches memories through the FTS5 table `memories_fts` that 0001
creates and keeps in sync with triggers. Postgres has no FTS5, so it gets the
equivalent it does have: a stored `tsvector` of the same two columns the FTS5
table indexes (content and category), with a GIN index over it.

Stored rather than computed per query, and `simple` rather than a language
configuration: memories are stored in whichever language the user wrote them,
so stemming them as if they were all English would help some and hurt others.

`memories.search_vector` has no model column; `pincer.db.metadata` excludes it
from autogenerate, the same way the FTS5 table is excluded.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-20
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(category, ''))) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_memories_search ON memories USING GIN (search_vector)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS idx_memories_search")
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS search_vector")
