"""Key identities by a UUIDv7 and point every user reference at it.

Until now an identity was its `pincer_user_id` — a configured name or a
`usr_` id — and every table that belongs to one repeated that text. Renaming
an identity meant rewriting each of them, and `rename_identity` only reached
the identity tables, sessions and onboarding: a merged identity's schedules,
memories and calls stayed behind under the old name.

Now:

- `pincer_identity_profiles` gets an `id` primary key. `pincer_user_id` stays
  as a UNIQUE column: it is the name the CLI, the TOML, the API and a JWT's
  `sub` use, and renaming an identity changes only it.
- `pincer_channel_identities` is renamed `pincer_identity_channels`, next to
  the other `pincer_identity_*` tables.
- The identity tables — channels, phones, credentials and auth sessions —
  replace their text `pincer_user_id` with a `profile_id`. Channels, phones
  and credentials get an `id` primary key of their own; their old keys stay
  UNIQUE. Auth sessions point at the credentials row, as they did.
- The ten tables that name their user in text — schedules, event triggers,
  briefing configs, voice calls, call origins, outbound call logs, memories,
  entities, conversations and audit logs — keep that text and gain a nullable
  `profile_id`, filled where the text names a profile. It stays NULL for a
  sender with no profile: a `telegram:1` stand-in, a web visitor, `system`.
- `pincer_identity_channels` gains a nullable `alt_id`: the channel's other
  id for the same account, i.e. the phone number behind a WhatsApp LID. With
  it a LID link stands for the configured phone even when WhatsApp cannot be
  asked, so startup no longer swaps it back to the phone. No table holds the
  mapping yet, so existing links start without one; the contact's next
  message fills it in.

Rows of the identity tables that point at no profile are dropped: their
foreign keys only ever held on Postgres, and SQLite let them through.

On SQLite the ten user tables are rebuilt rather than given a plain `ADD
COLUMN`: SQLite does not report the `ON DELETE` of a foreign key declared on
an added column, so the schema would no longer match the models. Rebuilding
`pincer_memories` drops the triggers that keep `pincer_memories_fts` in step
with it, so they are re-created and the index rebuilt, as in 0018.

Rows the upgrade drops — identity rows naming no profile, signed-in sessions
with no credentials — are logged with their names. The downgrade restores the
text columns from the profiles, but not those rows; it drops rows whose
profile went since, which have no name to go back to.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-18
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

from pincer.db.migration_helpers import (
    drop_rows,
    drop_stale_batch_table,
    hold_transaction,
    pk_name,
    referencing_foreign_keys,
    require_foreign_key_aware_rename,
    swap_primary_key,
    unswap_primary_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

PROFILES = "pincer_identity_profiles"
CHANNELS = "pincer_identity_channels"
OLD_CHANNELS = "pincer_channel_identities"
PHONES = "pincer_identity_phones"
CREDENTIALS = "pincer_identity_credentials"
AUTH_SESSIONS = "pincer_auth_sessions"

#: (table, the index on its user column, the foreign key `profile_id` gets:
#: referred table, referred column, ON DELETE).
CHILDREN: tuple[tuple[str, str, str, str, str | None], ...] = (
    (CHANNELS, "idx_ci_pincer", PROFILES, "id", None),
    (PHONES, "idx_identity_phones_user", PROFILES, "id", "CASCADE"),
    (CREDENTIALS, "", PROFILES, "id", "CASCADE"),
    (AUTH_SESSIONS, "idx_auth_sessions_user", CREDENTIALS, "profile_id", "CASCADE"),
)

#: What the children's text column pointed at before, for the downgrade.
OLD_FOREIGN_KEYS: dict[str, tuple[str, str | None]] = {
    CHANNELS: (PROFILES, None),
    PHONES: (PROFILES, "CASCADE"),
    CREDENTIALS: (PROFILES, "CASCADE"),
    AUTH_SESSIONS: (CREDENTIALS, "CASCADE"),
}

#: (table, old natural key that becomes UNIQUE, its creation time).
SWAPPED: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (CHANNELS, ("channel", "channel_user_id"), "uq_identity_channels_channel_channel_user_id"),
    (PHONES, ("phone",), "uq_identity_phones_phone"),
)

ALT_INDEX = "idx_identity_channels_alt"

PROFILE_UNIQUE = "uq_identity_profiles_pincer_user_id"
CREDENTIALS_UNIQUE = "uq_identity_credentials_profile_id"

#: Tables that name their user in text and gain a nullable `profile_id`.
OWNED: tuple[tuple[str, str], ...] = (
    ("pincer_schedules", "pincer_user_id"),
    ("pincer_event_triggers", "pincer_user_id"),
    ("pincer_briefing_configs", "pincer_user_id"),
    ("pincer_voice_calls", "pincer_user_id"),
    ("pincer_call_origins", "pincer_user_id"),
    ("pincer_outbound_call_logs", "user_id"),
    ("pincer_memories", "user_id"),
    ("pincer_entities", "user_id"),
    ("pincer_conversations", "user_id"),
    ("pincer_audit_logs", "user_id"),
)


#: `pincer_memories_fts`'s triggers, verbatim from 0019.
_FTS_TRIGGERS = {
    "memories_ai": """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON pincer_memories BEGIN
        INSERT INTO pincer_memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
    "memories_ad": """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON pincer_memories BEGIN
        INSERT INTO pincer_memories_fts(pincer_memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
    END
    """,
    "memories_au": """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON pincer_memories BEGIN
        INSERT INTO pincer_memories_fts(pincer_memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
        INSERT INTO pincer_memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
}


def fk_name(table: str) -> str:
    return f"fk_{table.removeprefix('pincer_')}_profile_id"


def owned_index(table: str) -> str:
    """The name SQLAlchemy gives `Column(..., index=True)`, so the models match."""
    return f"ix_{table}_profile_id"


def _rename(old: str, new: str) -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if old in existing and new not in existing:
        op.execute(f"ALTER TABLE {old} RENAME TO {new}")


def _drop_foreign_keys_onto(bind: sa.Connection, table: str) -> None:
    """Postgres only: it will not change a key a foreign key still points at."""
    for other, name in referencing_foreign_keys(bind, table):
        op.execute(f'ALTER TABLE {other} DROP CONSTRAINT "{name}"')


def upgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    postgres = bind.dialect.name == "postgresql"
    if not postgres:
        require_foreign_key_aware_rename(bind)
    _rename(OLD_CHANNELS, CHANNELS)

    if postgres:
        _drop_foreign_keys_onto(bind, CREDENTIALS)
        _drop_foreign_keys_onto(bind, PROFILES)
    swap_primary_key(bind, PROFILES, ["pincer_user_id"], time="created_at", kind="iso", unique=PROFILE_UNIQUE)

    # The children's text id becomes the profile's `id`. Text for now, on
    # both dialects, so the swaps below can key on it.
    for table, *_ in CHILDREN:
        op.add_column(table, sa.Column("profile_id", sa.Text(), nullable=True))
        bind.execute(
            sa.text(
                f"UPDATE {table} SET profile_id = "  # noqa: S608 - fixed identifiers
                f"(SELECT CAST(p.id AS TEXT) FROM {PROFILES} p WHERE p.pincer_user_id = {table}.pincer_user_id)"
            )
        )
        # An exact match: a link to `Alice` when the profile is `alice` goes too.
        drop_rows(bind, table, "profile_id IS NULL", key="pincer_user_id", reason="naming no profile")
    drop_rows(
        bind,
        AUTH_SESSIONS,
        f"profile_id NOT IN (SELECT profile_id FROM {CREDENTIALS})",
        key="pincer_user_id",
        reason="with no credentials",
    )

    for table, key, unique in SWAPPED:
        swap_primary_key(bind, table, list(key), time="created_at", kind="iso", unique=unique)
    swap_primary_key(bind, CREDENTIALS, ["profile_id"], time="created_at", kind="iso", unique=CREDENTIALS_UNIQUE)

    for table, index, *_ in CHILDREN:
        if postgres:
            if index:
                op.execute(f"DROP INDEX IF EXISTS {index}")
            op.execute(f"ALTER TABLE {table} DROP COLUMN pincer_user_id")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN profile_id TYPE uuid USING profile_id::uuid")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN profile_id SET NOT NULL")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                if index:
                    batch.drop_index(index)
                batch.drop_column("pincer_user_id")
                batch.alter_column("profile_id", existing_type=sa.Text(), nullable=False)
        if index:
            op.create_index(index, table, ["profile_id"])

    # Foreign keys last: auth sessions point at the credentials' `profile_id`,
    # which has to be a uuid already on Postgres.
    for table, _index, referred, referred_column, ondelete in CHILDREN:
        if postgres:
            op.create_foreign_key(fk_name(table), table, referred, ["profile_id"], [referred_column], ondelete=ondelete)
        else:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.create_foreign_key(fk_name(table), referred, ["profile_id"], [referred_column], ondelete=ondelete)

    with _memory_triggers_down(bind):
        for table, user_column in OWNED:
            if postgres:
                op.execute(
                    f"ALTER TABLE {table} ADD COLUMN profile_id uuid "
                    f"CONSTRAINT {fk_name(table)} REFERENCES {PROFILES}(id) ON DELETE SET NULL"
                )
            else:
                drop_stale_batch_table(table)
                with op.batch_alter_table(table, recreate="always") as batch:
                    batch.add_column(sa.Column("profile_id", sa.Text(), nullable=True))
                    batch.create_foreign_key(fk_name(table), PROFILES, ["profile_id"], ["id"], ondelete="SET NULL")
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET profile_id = "  # noqa: S608 - fixed identifiers
                    f"(SELECT p.id FROM {PROFILES} p WHERE p.pincer_user_id = {table}.{user_column})"
                )
            )
            op.create_index(owned_index(table), table, ["profile_id"])

    op.add_column(CHANNELS, sa.Column("alt_id", sa.Text(), nullable=True))
    op.create_index(ALT_INDEX, CHANNELS, ["channel", "alt_id"])


@contextmanager
def _memory_triggers_down(bind: sa.Connection) -> Iterator[None]:
    """On SQLite, rebuild `pincer_memories` with its FTS triggers out of the way.

    A batch rebuild drops the table the triggers hang on, and copies its rows
    into new rowids; re-creating the triggers and rebuilding the index after
    puts the full-text search back in step. Nothing to do on Postgres.
    """
    sqlite = bind.dialect.name == "sqlite"
    fts = sqlite and "pincer_memories_fts" in sa.inspect(bind).get_table_names()
    if fts:
        for trigger in _FTS_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    yield
    if fts:
        for ddl in _FTS_TRIGGERS.values():
            op.execute(ddl)
        op.execute("INSERT INTO pincer_memories_fts(pincer_memories_fts) VALUES('rebuild')")


def downgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    postgres = bind.dialect.name == "postgresql"

    op.drop_index(ALT_INDEX, table_name=CHANNELS)
    if postgres:
        op.execute(f"ALTER TABLE {CHANNELS} DROP COLUMN alt_id")
    else:
        drop_stale_batch_table(CHANNELS)
        with op.batch_alter_table(CHANNELS, recreate="always") as batch:
            batch.drop_column("alt_id")

    with _memory_triggers_down(bind):
        for table, _user_column in reversed(OWNED):
            op.drop_index(owned_index(table), table_name=table)
            if postgres:
                op.execute(f"ALTER TABLE {table} DROP COLUMN profile_id")
            else:
                drop_stale_batch_table(table)
                with op.batch_alter_table(table, recreate="always") as batch:
                    batch.drop_constraint(fk_name(table), type_="foreignkey")
                    batch.drop_column("profile_id")

    # The text id back from the profiles, while `profile_id` still says whose row it is.
    for table, *_ in CHILDREN:
        op.add_column(table, sa.Column("pincer_user_id", sa.Text(), nullable=True))
        bind.execute(
            sa.text(
                f"UPDATE {table} SET pincer_user_id = "  # noqa: S608 - fixed identifiers
                f"(SELECT p.pincer_user_id FROM {PROFILES} p WHERE p.id = {table}.profile_id)"
            )
        )
        # SQLite never enforced the foreign key: a row whose profile is gone
        # has no name to go back to, and the column turns NOT NULL below.
        drop_rows(bind, table, "pincer_user_id IS NULL", key="profile_id", reason="whose profile is gone")

    # Children before parents: auth sessions hold a foreign key onto the
    # credentials' `profile_id`, and every child one onto the profiles' `id`.
    for table, index, *_ in reversed(CHILDREN):
        if index:
            op.drop_index(index, table_name=table)
        if postgres:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {fk_name(table)}")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.drop_constraint(fk_name(table), type_="foreignkey")

    _unswap_credentials(bind, postgres)
    for table, key, unique in reversed(SWAPPED):
        unswap_primary_key(bind, table, list(key), unique=unique)
    for table in (AUTH_SESSIONS, PHONES, CHANNELS):
        if postgres:
            op.execute(f"ALTER TABLE {table} DROP COLUMN profile_id")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN pincer_user_id SET NOT NULL")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.drop_column("profile_id")
                batch.alter_column("pincer_user_id", existing_type=sa.Text(), nullable=False)

    unswap_primary_key(bind, PROFILES, ["pincer_user_id"], unique=PROFILE_UNIQUE)

    for table, index, *_ in CHILDREN:
        if index:
            op.create_index(index, table, ["pincer_user_id"])
        referred, ondelete = OLD_FOREIGN_KEYS[table]
        name = f"{table}_pincer_user_id_fkey"
        if postgres:
            op.create_foreign_key(name, table, referred, ["pincer_user_id"], ["pincer_user_id"], ondelete=ondelete)
        else:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.create_foreign_key(name, referred, ["pincer_user_id"], ["pincer_user_id"], ondelete=ondelete)

    _rename(CHANNELS, OLD_CHANNELS)


def _unswap_credentials(bind: sa.Connection, postgres: bool) -> None:
    """Credentials were keyed by the text id, not by what became their UNIQUE column."""
    if postgres:
        op.execute(f"ALTER TABLE {CREDENTIALS} DROP CONSTRAINT {CREDENTIALS_UNIQUE}")
        op.execute(f"ALTER TABLE {CREDENTIALS} DROP CONSTRAINT {pk_name(CREDENTIALS)}")
        op.execute(f"ALTER TABLE {CREDENTIALS} DROP COLUMN id")
        op.execute(f"ALTER TABLE {CREDENTIALS} DROP COLUMN profile_id")
        op.execute(f"ALTER TABLE {CREDENTIALS} ADD CONSTRAINT {pk_name(CREDENTIALS)} PRIMARY KEY (pincer_user_id)")
        return
    drop_stale_batch_table(CREDENTIALS)
    with op.batch_alter_table(CREDENTIALS, recreate="always") as batch:
        batch.drop_constraint(CREDENTIALS_UNIQUE, type_="unique")
        batch.drop_constraint(pk_name(CREDENTIALS), type_="primary")
        batch.drop_column("id")
        batch.drop_column("profile_id")
        batch.alter_column("pincer_user_id", existing_type=sa.Text(), nullable=False)
        batch.create_primary_key(pk_name(CREDENTIALS), ["pincer_user_id"])
