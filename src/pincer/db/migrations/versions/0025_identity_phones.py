"""Add `pincer_identity_phones` (issue #299).

The phone numbers of an identity. A voice call whose remote number is a
confirmed phone of an identity is treated as that identity's own call, not a
guest's.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import IsoText

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_identity_phones",
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("pincer_user_id", sa.Text(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("confirmed_at", IsoText(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["pincer_user_id"], ["pincer_identity_profiles.pincer_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("phone"),
    )
    op.create_index("idx_identity_phones_user", "pincer_identity_phones", ["pincer_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_identity_phones_user", table_name="pincer_identity_phones")
    op.drop_table("pincer_identity_phones")
