"""Drop the six dormant tables 0001 folded in from never-applied SQL files.

`registry_skills`, `expenses`, `habits`, `habit_checkins`, `pomodoro_sessions`
and `discord_threads` were kept by 0001 only so the old migration files'
schema was not thrown away. Nothing has read or written them since, four of
them still use the autoincrement integer keys 0017 removed everywhere else,
and `expenses` holds a foreign key to the identity profiles that 0033 would
otherwise have to carry through a key change. They go first, and alone.

The downgrade recreates them empty, in the shape they had at 0030: `pincer_`
names (0019), `expenses` pointing at `pincer_identity_profiles` (0011) and its
`amount` in DOUBLE PRECISION on Postgres (0013). Their rows are not restored.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

#: Children before parents: `habit_checkins` references `habits`.
TABLES: tuple[str, ...] = (
    "pincer_habit_checkins",
    "pincer_habits",
    "pincer_expenses",
    "pincer_pomodoro_sessions",
    "pincer_registry_skills",
    "pincer_discord_threads",
)

_AUTOPK = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgresql": "SERIAL PRIMARY KEY"}
_NOW_COL = {"sqlite": "TEXT", "postgresql": "TIMESTAMP"}
_AMOUNT = {"sqlite": "REAL", "postgresql": "DOUBLE PRECISION"}

#: 0001's DDL as it stood at 0030, parents first.
_CREATE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pincer_registry_skills (
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
    CREATE TABLE IF NOT EXISTS pincer_expenses (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL REFERENCES pincer_identity_profiles(pincer_user_id),
        amount {AMOUNT} NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        category TEXT NOT NULL DEFAULT 'general',
        description TEXT DEFAULT '',
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_expenses_user ON pincer_expenses(pincer_user_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS pincer_habits (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL,
        habit_name TEXT NOT NULL,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(pincer_user_id, habit_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pincer_habit_checkins (
        id {AUTOPK},
        habit_id INTEGER NOT NULL REFERENCES pincer_habits(id) ON DELETE CASCADE,
        checked_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        note TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pincer_pomodoro_sessions (
        id {AUTOPK},
        pincer_user_id TEXT NOT NULL,
        task TEXT NOT NULL DEFAULT 'Focus session',
        duration_min INTEGER NOT NULL DEFAULT 25,
        started_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP,
        completed INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pomodoro_user ON pincer_pomodoro_sessions(pincer_user_id, started_at)",
    """
    CREATE TABLE IF NOT EXISTS pincer_discord_threads (
        thread_id TEXT PRIMARY KEY,
        guild_id TEXT,
        pincer_user_id TEXT,
        session_id TEXT,
        created_at {NOW_COL} DEFAULT CURRENT_TIMESTAMP
    )
    """,
)

#: Same per-dialect expression index as 0001 (see its comment on why the
#: Postgres form has to cast the timestamp itself).
_HABIT_CHECKINS_DAILY = {
    "sqlite": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_checkins_daily "
        "ON pincer_habit_checkins(habit_id, date(checked_at))"
    ),
    "postgresql": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_checkins_daily "
        "ON pincer_habit_checkins(habit_id, (checked_at::date))"
    ),
}


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for template in _CREATE:
        op.execute(
            template.replace("{AUTOPK}", _AUTOPK[dialect])
            .replace("{NOW_COL}", _NOW_COL[dialect])
            .replace("{AMOUNT}", _AMOUNT[dialect])
        )
    op.execute(_HABIT_CHECKINS_DAILY[dialect])
