"""Add per-call voice analytics persistence.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_NOW_COL = {
    "sqlite": "TEXT",
    "postgresql": "TIMESTAMP",
}

_CALL_ANALYTICS = """
CREATE TABLE IF NOT EXISTS call_analytics (
    call_sid TEXT PRIMARY KEY REFERENCES voice_calls(call_sid),
    agent_speech_ms INTEGER,
    caller_speech_ms INTEGER,
    silence_ms INTEGER,
    overlap_ms INTEGER,
    interruptions INTEGER DEFAULT 0,
    talk_ratio REAL,
    method TEXT NOT NULL,
    sentiment TEXT,
    sentiment_trajectory TEXT,
    sentiment_rationale TEXT,
    sentiment_reason TEXT DEFAULT '',
    created_at {NOW_COL} NOT NULL
)
"""


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect not in _NOW_COL:
        raise RuntimeError(
            f"Unsupported database dialect for voice migrations: {dialect}"
        )

    op.execute(
        _CALL_ANALYTICS.replace(
            "{NOW_COL}",
            _NOW_COL[dialect],
        )
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_call_analytics_sentiment "
        "ON call_analytics(sentiment)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS call_analytics")
