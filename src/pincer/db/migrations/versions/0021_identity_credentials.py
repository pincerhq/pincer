"""Add `pincer_identity_credentials` (issue #228).

One row per identity that can authenticate to the REST API: an Argon2id
password hash, the SHA-256 of its API key with the few characters shown in the
masked form, and the `token_version` that invalidates its JWTs when the
password changes.

A table of its own rather than columns on `pincer_identity_profiles`: the
profile is reflected column by column into the identity API and copied per
column on rename, so a hash stored there would leak or be lost.

The unique index is what makes the API key lookup a single probe; a NULL
`api_key_hash` (an identity with only a password) is not unique-constrained on
either dialect.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.types import IsoText

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincer_identity_credentials",
        sa.Column("pincer_user_id", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_updated_at", IsoText(), nullable=True),
        sa.Column("api_key_hash", sa.Text(), nullable=True),
        sa.Column("api_key_prefix", sa.Text(), nullable=True),
        sa.Column("api_key_last4", sa.Text(), nullable=True),
        sa.Column("api_key_created_at", IsoText(), nullable=True),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["pincer_user_id"], ["pincer_identity_profiles.pincer_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pincer_user_id"),
    )
    op.create_index("idx_identity_credentials_api_key", "pincer_identity_credentials", ["api_key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_identity_credentials_api_key", table_name="pincer_identity_credentials")
    op.drop_table("pincer_identity_credentials")
