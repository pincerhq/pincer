"""Add persisted voice call briefing.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import inspect, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _columns(bind: Connection, table: str) -> set[str]:
    return {str(col["name"]) for col in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(
            f"Unsupported database dialect for voice migrations: {dialect}"
        )

    if "briefing_json" in _columns(bind, "voice_calls"):
        return

    if dialect == "postgresql":
        bind.execute(
            text(
                "ALTER TABLE voice_calls "
                "ADD COLUMN IF NOT EXISTS briefing_json TEXT DEFAULT ''"
            )
        )
    else:
        bind.execute(
            text(
                "ALTER TABLE voice_calls "
                "ADD COLUMN briefing_json TEXT DEFAULT ''"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE voice_calls "
            "DROP COLUMN IF EXISTS briefing_json"
        )

    # SQLite: keep the additive column. `downgrade base` ultimately drops
    # voice_calls in 0001, so rebuilding the whole table is unnecessary.
