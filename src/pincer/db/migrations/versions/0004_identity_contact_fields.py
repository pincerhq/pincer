"""Add email/timezone columns to identity_meta.

Populated only via the [identity] TOML config section (see
pincer.config.identity) — PINCER_IDENTITY_MAP's string grammar
(`name@ch1:id1=ch2:id2...`) has no field for these, so entries seeded from
the env var leave them NULL.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute("ALTER TABLE identity_meta ADD COLUMN IF NOT EXISTS email TEXT")
        op.execute("ALTER TABLE identity_meta ADD COLUMN IF NOT EXISTS timezone TEXT")
        return

    existing = {row[1] for row in bind.execute(text("PRAGMA table_info(identity_meta)")).fetchall()}
    if "email" not in existing:
        op.execute("ALTER TABLE identity_meta ADD COLUMN email TEXT")
    if "timezone" not in existing:
        op.execute("ALTER TABLE identity_meta ADD COLUMN timezone TEXT")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute("ALTER TABLE identity_meta DROP COLUMN IF EXISTS email")
        op.execute("ALTER TABLE identity_meta DROP COLUMN IF EXISTS timezone")
        return

    # SQLite can't drop columns without a full table rebuild; not worth it
    # here since 0001's downgrade("base") already drops identity_meta
    # entirely, taking these columns with it.
