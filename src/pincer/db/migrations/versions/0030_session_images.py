"""Add `pincer_session_images` (issue #358).

The photos of a conversation, kept so the model sees them on every later
request. One row per distinct photo per session, written once; the session's
messages hold only `{sha256, media_type}` refs. The foreign key cascades on
delete and on update: Postgres follows a deleted or re-keyed session on its
own, and SQLite (foreign keys off) is told by the code that does it.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import Real

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_session_images",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", Real(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["pincer_sessions.session_id"], ondelete="CASCADE", onupdate="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "sha256"),
    )


def downgrade() -> None:
    op.drop_table("pincer_session_images")
