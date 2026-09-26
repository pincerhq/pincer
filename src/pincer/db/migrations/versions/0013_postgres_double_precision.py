"""Widen every REAL column to DOUBLE PRECISION on Postgres.

Postgres' `REAL` is a 4-byte float. The epoch-second timestamps stored in
these columns (cost_logs, sessions, memories, ...) lose all precision below
128 seconds at today's epoch, and money loses cents after about $100k. Found
when a cost at 23:59:59 UTC was counted on the next day. SQLite's `REAL` is
already 8 bytes, so there this revision does nothing.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

#: Every REAL column at 0012, including the dormant `expenses.amount`.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_logs", "cost_usd"),
    ("call_analytics", "talk_ratio"),
    ("call_costs", "twilio_usd"),
    ("call_costs", "stt_seconds"),
    ("call_costs", "stt_usd"),
    ("call_costs", "tts_usd"),
    ("call_costs", "llm_usd"),
    ("call_costs", "total_usd"),
    ("call_transcripts", "confidence"),
    ("canary_runs", "duration_s"),
    ("conversations", "created_at"),
    ("conversations", "updated_at"),
    ("cost_logs", "timestamp"),
    ("cost_logs", "cost_usd"),
    ("entities", "last_seen"),
    ("expenses", "amount"),
    ("image_cost_logs", "timestamp"),
    ("image_cost_logs", "cost_usd"),
    ("memories", "created_at"),
    ("sessions", "created_at"),
    ("sessions", "updated_at"),
    ("telephony_calls", "duration_ms"),
    ("telephony_calls", "setup_ms"),
    ("telephony_calls", "media_establish_ms"),
    ("telephony_calls", "sample_rate_used"),
    ("telephony_spans", "duration_ms"),
    ("telephony_turns", "response_latency_ms"),
    ("telephony_turns", "endpointing_ms"),
    ("telephony_turns", "stt_first_partial_ms"),
    ("telephony_turns", "stt_final_ms"),
    ("telephony_turns", "agent_queue_ms"),
    ("telephony_turns", "agent_prep_ms"),
    ("telephony_turns", "llm_ttft_ms"),
    ("telephony_turns", "llm_total_ms"),
    ("telephony_turns", "tool_total_ms"),
    ("telephony_turns", "tts_first_audio_ms"),
    ("telephony_turns", "tts_total_ms"),
    ("telephony_turns", "audio_queue_ms"),
    ("telephony_turns", "total_ms"),
    ("telephony_turns", "bottleneck_ms"),
)


def _retype(to: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = set(sa.inspect(bind).get_table_names())
    for table, column in COLUMNS:
        if table in existing:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {to}")


def upgrade() -> None:
    _retype("DOUBLE PRECISION")


def downgrade() -> None:
    _retype("REAL")
