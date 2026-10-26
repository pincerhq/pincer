"""Add `pincer_onboarding` (PR #244).

Onboarding state per user and channel, out of the session metadata: which
session asked the onboarding question, when, how many replies held no answer,
and when it was closed. A user with no row falls back to the flags their
sessions carried before this.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import Real

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_onboarding",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("asked_session_id", sa.Text(), nullable=True),
        sa.Column("asked_at", Real(), nullable=True),
        sa.Column("completed_at", Real(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "channel"),
    )


def downgrade() -> None:
    op.drop_table("pincer_onboarding")
