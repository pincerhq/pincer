"""Prefix every Pincer table with `pincer_`.

Namespaces the schema so it is safe to share a database (or a Postgres
cluster) with other applications: every table Pincer owns is renamed in
place, so rows, indexes and triggers carry over — the same approach as
`0011_plural_table_names.py`. Foreign keys follow the rename automatically on
Postgres; SQLite does it only from 3.26 on, with `legacy_alter_table` off (its
default), so the migration refuses to run otherwise rather than leave a
reference dangling.

Index names, trigger names and foreign-key constraint names are left as they
are, per `0011`'s own precedent: they keep working against the renamed
tables, just with a name that no longer echoes it.

`alembic_version` is Alembic's own bookkeeping table, not one of these — it
is renamed separately by a bootstrap in `env.py`, run before Alembic ever
reads the version table. A migration cannot make that change itself: Alembic
decides which table to read from its `env.py` configuration, not from the
database, so renaming it mid-migration would mean either stamping this
revision into a table that no longer exists, or `env.py` looking for a table
that doesn't exist yet and concluding the database needs every revision
replayed from scratch.

`memories_fts` (SQLite's FTS5 index over `memories`) is not simply renamed
alongside its content table: an FTS5 table binds to its content table by
name at `CREATE` time (`content=memories`), and that binding does not follow
a rename of either table. It is dropped and recreated against the renamed
content table instead, then rebuilt from it — the same `INSERT INTO
fts(fts) VALUES('rebuild')` step `0018` already uses after rewriting
`memories`' ids.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

#: (old, new). Every table Pincer owns, except `memories_fts` (handled
#: separately below) and `alembic_version` (handled in `env.py`).
RENAMES: tuple[tuple[str, str], ...] = tuple(
    (name, f"pincer_{name}")
    for name in (
        # Modeled tables (`src/pincer/models/`).
        "audit_logs",
        "cost_logs",
        "image_cost_logs",
        "identity_profiles",
        "channel_identities",
        "conversations",
        "memories",
        "entities",
        "appointment_outcomes",
        "call_costs",
        "canary_runs",
        "schedules",
        "event_triggers",
        "briefing_configs",
        "sessions",
        "telephony_calls",
        "telephony_events",
        "telephony_spans",
        "telephony_turns",
        "voice_calls",
        "call_transcripts",
        "call_actions",
        "phone_contacts",
        "do_not_call_numbers",
        "outbound_call_logs",
        "inbound_messages",
        "call_threads",
        "call_thread_members",
        "call_analytics",
        # Dormant: created by 0001, read and written by nothing.
        "registry_skills",
        "expenses",
        "habits",
        "habit_checkins",
        "pomodoro_sessions",
        "discord_threads",
    )
)

_MIN_SQLITE = (3, 26, 0)

#: `memories_fts`'s triggers, verbatim from 0001/0018 but pointed at the
#: renamed tables.
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

#: The same triggers, verbatim from 0001, for `downgrade()`.
_OLD_FTS_TRIGGERS = {
    "memories_ai": """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
    "memories_ad": """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
    END
    """,
    "memories_au": """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
}


def _require_foreign_key_aware_rename(bind: sa.engine.Connection) -> None:
    version = bind.execute(sa.text("SELECT sqlite_version()")).scalar_one()
    if tuple(int(part) for part in str(version).split(".")[:3]) < _MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {version} cannot rename a table referenced by a foreign key; "
            f"{'.'.join(map(str, _MIN_SQLITE))} or newer is required"
        )
    if bind.execute(sa.text("PRAGMA legacy_alter_table")).scalar_one():
        raise RuntimeError("PRAGMA legacy_alter_table is ON; renaming would leave foreign keys dangling")


def _rename(pairs: tuple[tuple[str, str], ...]) -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for old, new in pairs:
        # Guarded both ways, so a database where the rename already
        # happened, or that never had the table, upgrades cleanly.
        if old in existing and new not in existing:
            op.execute(f"ALTER TABLE {old} RENAME TO {new}")


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        _require_foreign_key_aware_rename(bind)
        for trigger in _FTS_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        if "memories_fts" in sa.inspect(bind).get_table_names():
            op.execute("DROP TABLE memories_fts")

    _rename(RENAMES)

    if sqlite:
        existing = set(sa.inspect(bind).get_table_names())
        if "pincer_memories" in existing and "pincer_memories_fts" not in existing:
            op.execute(
                "CREATE VIRTUAL TABLE pincer_memories_fts USING fts5("
                "content, category, content=pincer_memories, content_rowid=rowid, "
                "tokenize='porter unicode61')"
            )
            for ddl in _FTS_TRIGGERS.values():
                op.execute(ddl)
            op.execute("INSERT INTO pincer_memories_fts(pincer_memories_fts) VALUES('rebuild')")


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        for trigger in _FTS_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        if "pincer_memories_fts" in sa.inspect(bind).get_table_names():
            op.execute("DROP TABLE pincer_memories_fts")

    _rename(tuple((new, old) for old, new in RENAMES))

    if sqlite:
        existing = set(sa.inspect(bind).get_table_names())
        if "memories" in existing and "memories_fts" not in existing:
            op.execute(
                "CREATE VIRTUAL TABLE memories_fts USING fts5("
                "content, category, content=memories, content_rowid=rowid, "
                "tokenize='porter unicode61')"
            )
            for ddl in _OLD_FTS_TRIGGERS.values():
                op.execute(ddl)
            op.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
