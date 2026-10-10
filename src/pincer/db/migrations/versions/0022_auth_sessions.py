"""Add `pincer_auth_sessions` (issue #228).

One row per signed-in dashboard session. Access and refresh tokens carry the
row's id; the row holds the id of the one refresh token that is currently
good, so refreshing invalidates the token it was made with and deleting the
row is a logout the server enforces.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import IsoText, Real, Uuid7

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_auth_sessions",
        sa.Column("id", Uuid7(), nullable=False),
        sa.Column("pincer_user_id", sa.Text(), nullable=False),
        sa.Column("refresh_jti", sa.Text(), nullable=False),
        sa.Column("prev_refresh_jti", sa.Text(), nullable=True),
        sa.Column("rotated_at", Real(), nullable=True),
        sa.Column("expires_at", Real(), nullable=False),
        sa.Column("created_at", IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["pincer_user_id"], ["pincer_identity_credentials.pincer_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_auth_sessions_user", "pincer_auth_sessions", ["pincer_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_auth_sessions_user", table_name="pincer_auth_sessions")
    op.drop_table("pincer_auth_sessions")
