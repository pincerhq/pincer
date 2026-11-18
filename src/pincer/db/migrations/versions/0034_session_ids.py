"""Give sessions, their photos and onboarding rows a UUIDv7 `id`.

The last three tables keyed by something other than an id of their own:

- `pincer_sessions` was keyed by its session key (`unified:alice`,
  `web:alice:<conversation>`). The key stays, UNIQUE: it is still how a
  session is found, and still what changes when its identity is renamed.
- `pincer_session_images` was keyed by (session key, hash), with a foreign key
  that had to cascade on update to follow a re-keyed session. It now points
  at the session's `id` (`session_ref`), which a re-key never changes, and
  (session, hash) stays UNIQUE. Photos of a session that is gone are dropped.
- `pincer_onboarding` was keyed by (user, channel), which stays UNIQUE.

Sessions and onboarding rows also gain a nullable `profile_id`, as the other
tables naming a user did in 0033.

Each id carries the row's own creation time: `created_at` for sessions and
photos, and for onboarding — which has none — when the question was asked or,
failing that, when the gate closed.

The downgrade restores the old keys; it is lossless.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.migration_helpers import (
    drop_stale_batch_table,
    pk_name,
    referencing_foreign_keys,
    swap_primary_key,
    unswap_primary_key,
)

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

SESSIONS = "pincer_sessions"
IMAGES = "pincer_session_images"
ONBOARDING = "pincer_onboarding"
PROFILES = "pincer_identity_profiles"

SESSIONS_UNIQUE = "uq_sessions_session_id"
IMAGES_UNIQUE = "uq_session_images_session_ref_sha256"
ONBOARDING_UNIQUE = "uq_onboarding_user_id_channel"
IMAGES_FK = "fk_session_images_session_ref"
OLD_IMAGES_FK = "pincer_session_images_session_id_fkey"

#: Tables that name their user in `user_id` and gain a nullable `profile_id`.
OWNED: tuple[str, ...] = (SESSIONS, ONBOARDING)


def fk_name(table: str) -> str:
    return f"fk_{table.removeprefix('pincer_')}_profile_id"


def owned_index(table: str) -> str:
    """The name SQLAlchemy gives `Column(..., index=True)`, so the models match."""
    return f"ix_{table}_profile_id"


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"

    if postgres:
        for other, name in referencing_foreign_keys(bind, SESSIONS):
            op.execute(f'ALTER TABLE {other} DROP CONSTRAINT "{name}"')
    swap_primary_key(bind, SESSIONS, ["session_id"], time="created_at", kind="epoch", unique=SESSIONS_UNIQUE)

    op.add_column(IMAGES, sa.Column("session_ref", sa.Text(), nullable=True))
    bind.execute(
        sa.text(
            f"UPDATE {IMAGES} SET session_ref = "  # noqa: S608 - fixed identifiers
            f"(SELECT CAST(s.id AS TEXT) FROM {SESSIONS} s WHERE s.session_id = {IMAGES}.session_id)"
        )
    )
    bind.execute(sa.text(f"DELETE FROM {IMAGES} WHERE session_ref IS NULL"))  # noqa: S608
    swap_primary_key(bind, IMAGES, ["session_ref", "sha256"], time="created_at", kind="epoch", unique=IMAGES_UNIQUE)
    if postgres:
        op.execute(f"ALTER TABLE {IMAGES} DROP COLUMN session_id")
        op.execute(f"ALTER TABLE {IMAGES} ALTER COLUMN session_ref TYPE uuid USING session_ref::uuid")
        op.create_foreign_key(IMAGES_FK, IMAGES, SESSIONS, ["session_ref"], ["id"], ondelete="CASCADE")
    else:
        drop_stale_batch_table(IMAGES)
        with op.batch_alter_table(IMAGES, recreate="always") as batch:
            batch.drop_column("session_id")
            batch.create_foreign_key(IMAGES_FK, SESSIONS, ["session_ref"], ["id"], ondelete="CASCADE")

    swap_primary_key(
        bind,
        ONBOARDING,
        ["user_id", "channel"],
        time="coalesce(asked_at, completed_at)",
        kind="epoch",
        unique=ONBOARDING_UNIQUE,
    )

    for table in OWNED:
        if postgres:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN profile_id uuid "
                f"CONSTRAINT {fk_name(table)} REFERENCES {PROFILES}(id) ON DELETE SET NULL"
            )
        else:
            # A rebuild, not an ADD COLUMN: SQLite does not report the ON
            # DELETE of a foreign key declared on an added column (see 0033).
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.add_column(sa.Column("profile_id", sa.Text(), nullable=True))
                batch.create_foreign_key(fk_name(table), PROFILES, ["profile_id"], ["id"], ondelete="SET NULL")
        bind.execute(
            sa.text(
                f"UPDATE {table} SET profile_id = "  # noqa: S608 - fixed identifiers
                f"(SELECT p.id FROM {PROFILES} p WHERE p.pincer_user_id = {table}.user_id)"
            )
        )
        op.create_index(owned_index(table), table, ["profile_id"])


def downgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"

    for table in reversed(OWNED):
        op.drop_index(owned_index(table), table_name=table)
        if postgres:
            op.execute(f"ALTER TABLE {table} DROP COLUMN profile_id")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.drop_constraint(fk_name(table), type_="foreignkey")
                batch.drop_column("profile_id")

    unswap_primary_key(bind, ONBOARDING, ["user_id", "channel"], unique=ONBOARDING_UNIQUE)

    # The photos name their session by key again, read back through `session_ref`.
    op.add_column(IMAGES, sa.Column("session_id", sa.Text(), nullable=True))
    bind.execute(
        sa.text(
            f"UPDATE {IMAGES} SET session_id = "  # noqa: S608 - fixed identifiers
            f"(SELECT s.session_id FROM {SESSIONS} s WHERE s.id = {IMAGES}.session_ref)"
        )
    )
    if postgres:
        op.execute(f"ALTER TABLE {IMAGES} DROP CONSTRAINT {IMAGES_FK}")
        op.execute(f"ALTER TABLE {IMAGES} DROP CONSTRAINT {IMAGES_UNIQUE}")
        op.execute(f"ALTER TABLE {IMAGES} DROP CONSTRAINT {pk_name(IMAGES)}")
        op.execute(f"ALTER TABLE {IMAGES} DROP COLUMN id")
        op.execute(f"ALTER TABLE {IMAGES} DROP COLUMN session_ref")
        op.execute(f"ALTER TABLE {IMAGES} ALTER COLUMN session_id SET NOT NULL")
        op.execute(f"ALTER TABLE {IMAGES} ADD CONSTRAINT {pk_name(IMAGES)} PRIMARY KEY (session_id, sha256)")
    else:
        drop_stale_batch_table(IMAGES)
        with op.batch_alter_table(IMAGES, recreate="always") as batch:
            batch.drop_constraint(IMAGES_FK, type_="foreignkey")
            batch.drop_constraint(IMAGES_UNIQUE, type_="unique")
            batch.drop_constraint(pk_name(IMAGES), type_="primary")
            batch.drop_column("id")
            batch.drop_column("session_ref")
            batch.alter_column("session_id", existing_type=sa.Text(), nullable=False)
            batch.create_primary_key(pk_name(IMAGES), ["session_id", "sha256"])

    unswap_primary_key(bind, SESSIONS, ["session_id"], unique=SESSIONS_UNIQUE)

    if postgres:
        op.create_foreign_key(
            OLD_IMAGES_FK, IMAGES, SESSIONS, ["session_id"], ["session_id"], ondelete="CASCADE", onupdate="CASCADE"
        )
    else:
        with op.batch_alter_table(IMAGES, recreate="always") as batch:
            batch.create_foreign_key(
                OLD_IMAGES_FK, SESSIONS, ["session_id"], ["session_id"], ondelete="CASCADE", onupdate="CASCADE"
            )
