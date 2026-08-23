"""Baseline schema.

Captures the schema Pincer previously created via inline
``CREATE TABLE IF NOT EXISTS`` calls scattered across `memory/sqlite.py`,
`core/session.py`, `core/identity.py`, `security/audit.py`,
`scheduler/cron.py`, `scheduler/triggers.py`, `scheduler/proactive.py` and
`llm/cost_tracker.py` — all of which now go through Alembic instead. Also
folds in the tables from the old, never-applied `data/migrations/*.sql`
files (skill_registry, expenses, habits, habit_checkins, pomodoro_sessions,
discord_threads, voice_calls, call_transcripts, call_actions,
phone_contacts). The legacy `identity_map` table (superseded by
`identity_meta`/`channel_identities`) is intentionally not recreated here —
see revision 0002 for its one-time data migration.

Written as raw SQL (`op.execute()`) rather than SQLAlchemy-metadata-driven
DDL so it stays dialect-agnostic. Most statements are portable verbatim;
where SQLite and Postgres genuinely diverge (autoincrement PKs, blob
storage, and SQLite's FTS5 full-text index) a small per-dialect token swap
or an explicit branch is used instead of two full copies of every table.

Revision ID: 0001
Revises:
Create Date: 2026-08-22
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Per-dialect tokens substituted into the (mostly dialect-agnostic) table
# templates below. Everything else — TEXT, INTEGER, REAL, BOOLEAN,
# TIMESTAMP, `CURRENT_TIMESTAMP` defaults, `IF NOT EXISTS` — is valid
# verbatim SQL on both SQLite and Postgres.
_AUTOPK = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgresql": "SERIAL PRIMARY KEY"}
_BLOB = {"sqlite": "BLOB", "postgresql": "BYTEA"}

# Postgres has no assignment cast from `timestamptz` to `text`, so a column
# typed TEXT with a bare `DEFAULT CURRENT_TIMESTAMP` fails at CREATE TABLE
# time there. The `CURRENT_TIMESTAMP` default expression itself is valid
# verbatim on both dialects once the column type matches it, so only the
# type needs to flip per dialect (mirroring `phone_contacts.created_at`
# below, which is already `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` on both).
_NOW_COL = {"sqlite": "TEXT", "postgresql": "TIMESTAMP"}


def _sql(template: str, dialect: str) -> str:
    return (
        template.replace("{AUTOPK}", _AUTOPK[dialect])
        .replace("{BLOB}", _BLOB[dialect])
        .replace("{NOW_COL}", _NOW_COL[dialect])
    )


# ── memory/sqlite.py ─────────────────────────────────────────────
_MEMORY_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        messages_json TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, channel)",
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        tags TEXT NOT NULL DEFAULT '[]',
        embedding_blob {BLOB},
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, category)",
    """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        attributes_json TEXT NOT NULL DEFAULT '{}',
        last_seen REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ent_user ON entities(user_id, type)",
]

# FTS5 is a SQLite extension with no Postgres equivalent — a real Postgres
# full-text search implementation (tsvector + GIN index + differently-shaped
# triggers) is separate future work for whenever Postgres runtime support
# actually lands. The Postgres branch below is a deliberate no-op, not a
# placeholder for "not implemented yet by accident".
_MEMORY_FTS_SQLITE = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, category,
        content=memories, content_rowid=rowid,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
]

# ── core/session.py ──────────────────────────────────────────────
_SESSION_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        messages TEXT NOT NULL DEFAULT '[]',
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_user ON sessions(user_id, channel)",
]

# ── core/identity.py ─────────────────────────────────────────────
# Includes active_channel/active_channel_updated_at directly (previously
# added via a separate ALTER TABLE in _migrate_active_channel) since this
# baseline represents the current desired schema, not its historical shape.
_IDENTITY_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS identity_meta (
        pincer_user_id TEXT PRIMARY KEY,
        preferred_channel TEXT,
        display_name TEXT,
        active_channel TEXT,
        active_channel_updated_at TEXT,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_identities (
        channel TEXT NOT NULL,
        channel_user_id TEXT NOT NULL,
        pincer_user_id TEXT NOT NULL REFERENCES identity_meta(pincer_user_id),
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (channel, channel_user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ci_pincer ON channel_identities(pincer_user_id)",
]

# ── security/audit.py ────────────────────────────────────────────
_AUDIT_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id {AUTOPK},
        timestamp TEXT NOT NULL,
        user_id TEXT NOT NULL,
        session_id TEXT,
        action TEXT NOT NULL,
        tool TEXT,
        input_summary TEXT,
        output_summary TEXT,
        approved INTEGER DEFAULT 1,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER,
        ip_address TEXT,
        channel TEXT,
        metadata_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)",
    "CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool)",
]

# ── scheduler/cron.py, triggers.py, proactive.py ─────────────────
_SCHEDULER_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        cron_expr TEXT NOT NULL,
        action TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'telegram',
        timezone TEXT NOT NULL DEFAULT 'UTC',
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        next_run_at TEXT,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        updated_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(next_run_at) WHERE enabled = 1",
    """
    CREATE TABLE IF NOT EXISTS event_triggers (
        id {AUTOPK},
        trigger_type TEXT NOT NULL,
        trigger_key TEXT NOT NULL,
        pincer_user_id TEXT NOT NULL,
        processed_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        result TEXT,
        UNIQUE(trigger_type, trigger_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS briefing_config (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL UNIQUE,
        sections TEXT NOT NULL DEFAULT '["weather","calendar","email","news"]',
        custom_sections TEXT DEFAULT '[]',
        weather_location TEXT DEFAULT 'Berlin,DE',
        news_topics TEXT DEFAULT '["technology","business"]',
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        updated_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

# ── llm/cost_tracker.py ──────────────────────────────────────────
_COST_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS cost_log (
        id {AUTOPK},
        timestamp REAL NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        session_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cost_timestamp ON cost_log(timestamp)",
    """
    CREATE TABLE IF NOT EXISTS image_cost_log (
        id {AUTOPK},
        timestamp REAL NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        cost_usd REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_image_cost_timestamp ON image_cost_log(timestamp)",
]

# ── folded from data/migrations/004_sprint4.sql ──────────────────
# Dormant schema — nothing in the current codebase reads or writes these
# tables. Folded in per an explicit decision to preserve them rather than
# discard the never-applied migration files, not because anything depends
# on them existing. `expenses.pincer_user_id` now references `identity_meta`
# instead of the superseded `identity_map`.
_SKILLS_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS skill_registry (
        skill_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        description TEXT,
        author TEXT DEFAULT 'unknown',
        safety_score INTEGER DEFAULT 100,
        install_path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        installed_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        updated_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL REFERENCES identity_meta(pincer_user_id),
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        category TEXT NOT NULL DEFAULT 'general',
        description TEXT DEFAULT '',
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_expenses_user ON expenses(pincer_user_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS habits (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL,
        habit_name TEXT NOT NULL,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(pincer_user_id, habit_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_checkins (
        id {AUTOPK},
        habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
        checked_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        note TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pomodoro_sessions (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL,
        task TEXT NOT NULL DEFAULT 'Focus session',
        duration_min INTEGER NOT NULL DEFAULT 25,
        started_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        completed INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pomodoro_user ON pomodoro_sessions(pincer_user_id, started_at)",
    """
    CREATE TABLE IF NOT EXISTS discord_threads (
        thread_id TEXT PRIMARY KEY,
        guild_id TEXT,
        pincer_user_id TEXT,
        session_id TEXT,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

# One habit per calendar day, expressed as an expression index rather than a
# table-level UNIQUE(...) constraint (not portably supported there) — the
# underlying "current day" expression itself still differs per dialect.
#
# `checked_at` is TEXT on SQLite and TIMESTAMP (no timezone) on Postgres (see
# `_NOW_COL`). On Postgres, casting a `timestamp without time zone` to `date`
# is a pure truncation with no DateStyle/timezone dependency, so it's
# IMMUTABLE — unlike casting *text* to `date`, which depends on the
# session's DateStyle setting (parsing ambiguity) and is only STABLE, or
# casting the timestamp to text first, which depends on DateStyle for
# output formatting and is likewise only STABLE. `checked_at::date` is the
# only one of the three that Postgres accepts in an index expression.
_HABIT_CHECKINS_DAILY_UNIQUE = {
    "sqlite": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_checkins_daily ON habit_checkins(habit_id, date(checked_at))"
    ),
    "postgresql": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_checkins_daily ON habit_checkins(habit_id, (checked_at::date))"
    ),
}

# ── folded from data/migrations/005_sprint7_voice.sql ────────────
# Also dormant: voice_calls/phone_contacts have no readers or writers today;
# call_transcripts/call_actions have exactly one dormant writer
# (voice/transcript.py TranscriptLogger.save_to_db, itself never called).
_VOICE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS voice_calls (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        direction TEXT NOT NULL,
        caller_number TEXT NOT NULL,
        target_number TEXT,
        target_name TEXT,
        purpose TEXT,
        status TEXT NOT NULL,
        engine TEXT NOT NULL,
        started_at TIMESTAMP NOT NULL,
        ended_at TIMESTAMP,
        duration_seconds INTEGER,
        recording_url TEXT,
        recording_consent BOOLEAN DEFAULT FALSE,
        session_id TEXT,
        metadata_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_voice_calls_user ON voice_calls(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_voice_calls_status ON voice_calls(status)",
    """
    CREATE TABLE IF NOT EXISTS call_transcripts (
        id {AUTOPK},
        call_id TEXT NOT NULL REFERENCES voice_calls(id),
        speaker TEXT NOT NULL,
        text TEXT NOT NULL,
        confidence REAL,
        is_final BOOLEAN DEFAULT TRUE,
        state TEXT,
        timestamp TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_transcripts_call ON call_transcripts(call_id)",
    """
    CREATE TABLE IF NOT EXISTS call_actions (
        id {AUTOPK},
        call_id TEXT NOT NULL REFERENCES voice_calls(id),
        action_type TEXT NOT NULL,
        tool_name TEXT,
        input_summary TEXT,
        output_summary TEXT,
        user_confirmed BOOLEAN,
        timestamp TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_actions_call ON call_actions(call_id)",
    """
    CREATE TABLE IF NOT EXISTS phone_contacts (
        id {AUTOPK},
        user_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        phone_number TEXT NOT NULL,
        category TEXT DEFAULT '',
        ivr_tree_json TEXT,
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_phone_contacts_user ON phone_contacts(user_id)",
]

# NOCASE is a SQLite built-in collation; Postgres has no equivalent by that
# name (case-insensitive lookups there would use lower(name) or citext),
# so this one index is dialect-branched rather than templated.
_PHONE_CONTACTS_NAME_INDEX = {
    "sqlite": "CREATE INDEX IF NOT EXISTS idx_phone_contacts_name ON phone_contacts(name COLLATE NOCASE)",
    "postgresql": "CREATE INDEX IF NOT EXISTS idx_phone_contacts_name ON phone_contacts(lower(name))",
}

_ALL_TEMPLATES = (
    _MEMORY_TABLES
    + _SESSION_TABLES
    + _IDENTITY_TABLES
    + _AUDIT_TABLES
    + _SCHEDULER_TABLES
    + _COST_TABLES
    + _SKILLS_TABLES
    + _VOICE_TABLES
)

# Reverse-dependency order for downgrade() (children before the tables they
# reference).
_ALL_TABLES_DROP_ORDER = [
    "call_actions",
    "call_transcripts",
    "voice_calls",
    "phone_contacts",
    "habit_checkins",
    "habits",
    "pomodoro_sessions",
    "discord_threads",
    "expenses",
    "skill_registry",
    "image_cost_log",
    "cost_log",
    "briefing_config",
    "event_triggers",
    "schedules",
    "audit_log",
    "channel_identities",
    "identity_meta",
    "sessions",
    "entities",
    "memories_fts",
    "memories",
    "conversations",
]


def _add_column_if_missing(bind: Connection, dialect: str, table: str, column: str, coldef: str) -> None:
    """Add `column` to `table` if it isn't already there.

    `CREATE TABLE IF NOT EXISTS` above is a no-op on a table that already
    exists — which is the normal case for every pre-existing install, since
    this baseline is retrofitted onto tables the app already created via
    inline DDL before Alembic existed. Two columns in particular
    (`identity_meta.active_channel`/`active_channel_updated_at`,
    `memories.tags`) used to be reconciled onto old databases by a runtime
    `ALTER TABLE ... ADD COLUMN` check on every `ensure_table()`/
    `initialize()` call; this reproduces that same guarantee once, here,
    instead.
    """
    if dialect == "postgresql":
        bind.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coldef}"))
        return
    existing = {row[1] for row in bind.execute(text(f"PRAGMA table_info({table})")).fetchall()}
    if column not in existing:
        bind.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for template in _ALL_TEMPLATES:
        op.execute(_sql(template, dialect))

    if dialect == "sqlite":
        for stmt in _MEMORY_FTS_SQLITE:
            op.execute(stmt)

    op.execute(_HABIT_CHECKINS_DAILY_UNIQUE[dialect])
    op.execute(_PHONE_CONTACTS_NAME_INDEX[dialect])

    _add_column_if_missing(bind, dialect, "identity_meta", "active_channel", "TEXT")
    _add_column_if_missing(bind, dialect, "identity_meta", "active_channel_updated_at", "TEXT")
    _add_column_if_missing(bind, dialect, "memories", "tags", "TEXT NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    for table in _ALL_TABLES_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
