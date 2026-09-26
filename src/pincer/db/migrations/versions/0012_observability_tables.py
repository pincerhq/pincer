"""Bring the three observability tables under Alembic.

`appointment_outcomes` (observability/bookings.py), `call_costs`
(observability/call_costs.py) and `canary_runs` (observability/canary.py) were
created at runtime by `executescript` on every connection, outside Alembic.
The DDL here is what those modules ran, so a database that already has the
tables keeps them untouched (`IF NOT EXISTS`). Timestamps and autoincrement ids
use the same dialect tokens as 0001/0005, so they are `TIMESTAMP` and `SERIAL`
on Postgres.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_AUTOPK = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgresql": "SERIAL PRIMARY KEY"}
_NOW_COL = {"sqlite": "TEXT", "postgresql": "TIMESTAMP"}

_APPOINTMENT_OUTCOMES = """
CREATE TABLE IF NOT EXISTS appointment_outcomes (
    id {AUTOPK},
    task_id TEXT NOT NULL,
    call_sid TEXT DEFAULT '',
    result TEXT NOT NULL,
    language TEXT DEFAULT '',
    attempts INTEGER DEFAULT 1,
    detail TEXT DEFAULT '',
    recorded_at {NOW_COL} NOT NULL
)
"""

_CALL_COSTS = """
CREATE TABLE IF NOT EXISTS call_costs (
    call_sid TEXT PRIMARY KEY,
    direction TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    language TEXT DEFAULT '',
    duration_seconds INTEGER DEFAULT 0,
    twilio_usd REAL DEFAULT 0.0,
    stt_seconds REAL DEFAULT 0.0,
    stt_usd REAL DEFAULT 0.0,
    tts_characters INTEGER DEFAULT 0,
    tts_usd REAL DEFAULT 0.0,
    llm_input_tokens INTEGER DEFAULT 0,
    llm_output_tokens INTEGER DEFAULT 0,
    llm_usd REAL DEFAULT 0.0,
    total_usd REAL DEFAULT 0.0,
    recorded_at {NOW_COL} NOT NULL
)
"""

_CANARY_RUNS = """
CREATE TABLE IF NOT EXISTS canary_runs (
    id {AUTOPK},
    ran_at {NOW_COL} NOT NULL,
    ok INTEGER NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    turns INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0.0
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_appointment_outcomes_recorded ON appointment_outcomes(recorded_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointment_outcomes_task ON appointment_outcomes(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_call_costs_recorded ON call_costs(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_canary_runs_ran_at ON canary_runs(ran_at)",
)


def _sql(template: str, dialect: str) -> str:
    return template.replace("{AUTOPK}", _AUTOPK[dialect]).replace("{NOW_COL}", _NOW_COL[dialect])


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    for template in (_APPOINTMENT_OUTCOMES, _CALL_COSTS, _CANARY_RUNS):
        op.execute(_sql(template, dialect))
    for index in _INDEXES:
        op.execute(index)


def downgrade() -> None:
    for table in ("canary_runs", "call_costs", "appointment_outcomes"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
