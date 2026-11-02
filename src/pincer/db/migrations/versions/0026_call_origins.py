"""Add `pincer_call_origins` (issue #303).

One row per outbound call: who asked for it, on which channel and in which
chat session, and the final report once it is delivered. The in-memory call
routing is rebuilt from it after a restart, and web chat reads its reports.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import IsoText

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_call_origins",
        sa.Column("call_sid", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("pincer_user_id", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("channel", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("session_key", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("language", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("target_number", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("target_name", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("purpose", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("instructions", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("created_at", IsoText(), nullable=False),
        sa.Column("report_outcome", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("report_text", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("reported_at", IsoText(), nullable=True),
        sa.PrimaryKeyConstraint("call_sid"),
    )
    op.create_index("idx_call_origins_session", "pincer_call_origins", ["pincer_user_id", "session_key"], unique=False)
    op.create_index("idx_call_origins_reported", "pincer_call_origins", ["reported_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_call_origins_reported", table_name="pincer_call_origins")
    op.drop_index("idx_call_origins_session", table_name="pincer_call_origins")
    op.drop_table("pincer_call_origins")
