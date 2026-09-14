"""Add inbound receptionist persistence.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import inspect, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_AUTOPK = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgresql": "SERIAL PRIMARY KEY",
}
_NOW_COL = {
    "sqlite": "TEXT",
    "postgresql": "TIMESTAMP",
}

_INBOUND_MESSAGES = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id {AUTOPK},
    call_sid TEXT NOT NULL,
    caller_name TEXT DEFAULT '',
    caller_name_unverified INTEGER DEFAULT 0,
    callback_number TEXT DEFAULT '',
    callback_unverified INTEGER DEFAULT 0,
    matter TEXT DEFAULT '',
    urgent INTEGER DEFAULT 0,
    created_at {NOW_COL} NOT NULL,
    delivered_to_owner_at {NOW_COL}
)
"""


def _sql(template: str, dialect: str) -> str:
    return template.replace("{AUTOPK}", _AUTOPK[dialect]).replace("{NOW_COL}", _NOW_COL[dialect])


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

    if dialect not in _AUTOPK:
        raise RuntimeError(f"Unsupported database dialect for voice migrations: {dialect}")

    op.execute(_sql(_INBOUND_MESSAGES, dialect))

    _add_column_if_missing(
        bind,
        dialect,
        "voice_calls",
        "inbound_intent",
        "TEXT DEFAULT ''",
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_inbound_messages_call ON inbound_messages(call_sid)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inbound_messages")

    # As with 0004/0005, SQLite does not rebuild voice_calls merely to remove
    # one additive column. `downgrade base` remains complete because 0001
    # ultimately drops voice_calls.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE voice_calls DROP COLUMN IF EXISTS inbound_intent")
