"""Add persistent voice call threads.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import inspect, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_NOW_COL = {
    "sqlite": "TEXT",
    "postgresql": "TIMESTAMP",
}

_CALL_THREADS = """
CREATE TABLE IF NOT EXISTS call_threads (
    thread_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    origin TEXT NOT NULL,
    primary_number TEXT DEFAULT '',
    contact_name TEXT DEFAULT '',
    language TEXT DEFAULT '',
    rolling_summary TEXT DEFAULT '',
    open_commitments TEXT DEFAULT '[]',
    created_at {NOW_COL} NOT NULL,
    updated_at {NOW_COL} NOT NULL,
    resolved_at {NOW_COL},
    closed_at {NOW_COL}
)
"""

_CALL_THREAD_MEMBERS = """
CREATE TABLE IF NOT EXISTS call_thread_members (
    call_sid TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES call_threads(thread_id),
    attach_kind TEXT NOT NULL DEFAULT '',
    attached_at {NOW_COL} NOT NULL,
    call_started_at {NOW_COL},
    direction TEXT DEFAULT '',
    outcome_code TEXT DEFAULT '',
    task_result TEXT DEFAULT ''
)
"""


def _sql(template: str, dialect: str) -> str:
    return template.replace("{NOW_COL}", _NOW_COL[dialect])


def _columns(bind: Connection, table: str) -> set[str]:
    return {str(col["name"]) for col in inspect(bind).get_columns(table)}


def _add_column_if_missing(
    bind: Connection,
    dialect: str,
    table: str,
    column: str,
    coldef: str,
) -> None:
    if column in _columns(bind, table):
        return

    if dialect == "postgresql":
        bind.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coldef}"))
    else:
        bind.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect not in _NOW_COL:
        raise RuntimeError(f"Unsupported database dialect for voice migrations: {dialect}")

    op.execute(_sql(_CALL_THREADS, dialect))
    op.execute(_sql(_CALL_THREAD_MEMBERS, dialect))

    _add_column_if_missing(
        bind,
        dialect,
        "voice_calls",
        "thread_id",
        "TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        bind,
        dialect,
        "voice_calls",
        "thread_attach_kind",
        "TEXT DEFAULT ''",
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_calls_thread ON voice_calls(thread_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_threads_number_status ON call_threads(primary_number, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_thread_members_thread ON call_thread_members(thread_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS call_thread_members")
    op.execute("DROP TABLE IF EXISTS call_threads")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE voice_calls DROP COLUMN IF EXISTS thread_attach_kind")
        op.execute("ALTER TABLE voice_calls DROP COLUMN IF EXISTS thread_id")
