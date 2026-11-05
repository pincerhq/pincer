"""Add `active` and `source` to `pincer_identity_profiles` (issue #348).

`active` switches an identity off without deleting it. `source` records where
an identity came from, so syncing the `[identity]` TOML removes only what the
TOML made: "config", "cli" (`pincer identity create`) or "auto" (a `usr_` id
made for an unknown sender).

Backfill: `usr_…` ids are "auto"; a profile with a credential and no channel
link is what `pincer identity create` made, so "cli"; everything else came
from the config.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pincer_identity_profiles") as batch:
        batch.add_column(sa.Column("active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False))
        batch.add_column(sa.Column("source", sa.Text(), server_default="config", nullable=False))

    op.execute("UPDATE pincer_identity_profiles SET source = 'auto' WHERE pincer_user_id LIKE 'usr\\_%' ESCAPE '\\'")
    op.execute(
        "UPDATE pincer_identity_profiles SET source = 'cli' "
        "WHERE source = 'config' "
        "AND pincer_user_id NOT IN (SELECT pincer_user_id FROM pincer_channel_identities) "
        "AND pincer_user_id IN ("
        "  SELECT pincer_user_id FROM pincer_identity_credentials "
        "  WHERE password_hash IS NOT NULL OR api_key_hash IS NOT NULL"
        ")"
    )


def downgrade() -> None:
    with op.batch_alter_table("pincer_identity_profiles") as batch:
        batch.drop_column("source")
        batch.drop_column("active")
