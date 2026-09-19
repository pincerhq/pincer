"""Add telephony telemetry storage (calls, events, spans, turn metrics).

Four tables, one per grain:

* ``telephony_calls``  — one row per call, the search/overview grain.
* ``telephony_events`` — point-in-time facts, the timeline grain.
* ``telephony_spans``  — intervals, the waterfall grain (they overlap).
* ``telephony_turns``  — per-turn derived latencies, the "which turn was slow" grain.

High-cardinality identifiers (call ids, SIDs, trace ids) live here, in traces
and logs, deliberately NOT as metric labels — see
`pincer/observability/metrics.py` for the same rule on the OTel side.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_NOW_COL = {
    "sqlite": "TEXT",
    "postgresql": "TIMESTAMP",
}

_CALLS = """
CREATE TABLE IF NOT EXISTS telephony_calls (
    call_id TEXT PRIMARY KEY,
    provider_call_id TEXT DEFAULT '',
    trace_id TEXT DEFAULT '',
    direction TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    transport TEXT DEFAULT '',
    codec TEXT DEFAULT '',
    sample_rate INTEGER DEFAULT 0,
    model TEXT DEFAULT '',
    language TEXT DEFAULT '',
    tenant_id TEXT DEFAULT '',
    environment TEXT DEFAULT '',
    app_version TEXT DEFAULT '',
    from_number_masked TEXT DEFAULT '',
    to_number_masked TEXT DEFAULT '',
    registered_at {NOW_COL},
    dialed_at {NOW_COL},
    answered_at {NOW_COL},
    media_open_at {NOW_COL},
    ended_at {NOW_COL},
    status TEXT DEFAULT 'active',
    outcome TEXT DEFAULT '',
    failure_category TEXT DEFAULT '',
    termination_reason TEXT DEFAULT '',
    failure_code TEXT DEFAULT '',
    duration_ms REAL,
    setup_ms REAL,
    media_establish_ms REAL,
    turn_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    timeout_count INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    interruption_count INTEGER DEFAULT 0,
    reconnect_count INTEGER DEFAULT 0,
    sampled INTEGER DEFAULT 1,
    sample_rate_used REAL DEFAULT 1.0,
    coverage TEXT DEFAULT 'full',
    config_json TEXT DEFAULT '{}',
    updated_at {NOW_COL}
)
"""

_EVENTS = """
CREATE TABLE IF NOT EXISTS telephony_events (
    event_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    provider_call_id TEXT DEFAULT '',
    trace_id TEXT DEFAULT '',
    span_id TEXT DEFAULT '',
    turn_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    ts_utc {NOW_COL} NOT NULL,
    mono_ns INTEGER DEFAULT 0,
    seq INTEGER DEFAULT 0,
    attributes TEXT DEFAULT '{}'
)
"""

_SPANS = """
CREATE TABLE IF NOT EXISTS telephony_spans (
    span_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    trace_id TEXT DEFAULT '',
    parent_span_id TEXT DEFAULT '',
    turn_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    start_utc {NOW_COL} NOT NULL,
    end_utc {NOW_COL},
    start_mono_ns INTEGER DEFAULT 0,
    end_mono_ns INTEGER,
    duration_ms REAL,
    status TEXT DEFAULT 'ok',
    attempt INTEGER DEFAULT 1,
    attributes TEXT DEFAULT '{}'
)
"""

_TURNS = """
CREATE TABLE IF NOT EXISTS telephony_turns (
    turn_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    turn_no INTEGER DEFAULT 0,
    trigger TEXT DEFAULT '',
    started_at {NOW_COL},
    first_audio_at {NOW_COL},
    engine TEXT DEFAULT '',
    model TEXT DEFAULT '',
    language TEXT DEFAULT '',
    streamed INTEGER DEFAULT 1,
    response_latency_ms REAL,
    response_latency_source TEXT DEFAULT '',
    endpointing_ms REAL,
    stt_first_partial_ms REAL,
    stt_final_ms REAL,
    agent_queue_ms REAL,
    agent_prep_ms REAL,
    llm_ttft_ms REAL,
    llm_total_ms REAL,
    tool_total_ms REAL,
    tts_first_audio_ms REAL,
    tts_total_ms REAL,
    audio_queue_ms REAL,
    total_ms REAL,
    tool_calls INTEGER DEFAULT 0,
    tool_retries INTEGER DEFAULT 0,
    tool_timeouts INTEGER DEFAULT 0,
    interrupted INTEGER DEFAULT 0,
    cancelled INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    bottleneck_stage TEXT DEFAULT '',
    bottleneck_ms REAL,
    critical_path TEXT DEFAULT '[]',
    complete INTEGER DEFAULT 1,
    created_at {NOW_COL} NOT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tel_calls_started ON telephony_calls(registered_at)",
    "CREATE INDEX IF NOT EXISTS idx_tel_calls_provider_sid ON telephony_calls(provider_call_id)",
    "CREATE INDEX IF NOT EXISTS idx_tel_calls_status ON telephony_calls(status)",
    "CREATE INDEX IF NOT EXISTS idx_tel_calls_tenant ON telephony_calls(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_tel_events_call ON telephony_events(call_id, ts_utc, seq)",
    "CREATE INDEX IF NOT EXISTS idx_tel_events_turn ON telephony_events(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_tel_events_name ON telephony_events(name)",
    "CREATE INDEX IF NOT EXISTS idx_tel_spans_call ON telephony_spans(call_id, start_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tel_spans_turn ON telephony_spans(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_tel_turns_call ON telephony_turns(call_id, turn_no)",
    "CREATE INDEX IF NOT EXISTS idx_tel_turns_created ON telephony_turns(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tel_turns_latency ON telephony_turns(response_latency_ms)",
)


def _sql(template: str, dialect: str) -> str:
    return template.replace("{NOW_COL}", _NOW_COL[dialect])


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in _NOW_COL:
        raise RuntimeError(f"Unsupported database dialect for telephony telemetry: {dialect}")

    for template in (_CALLS, _EVENTS, _SPANS, _TURNS):
        op.execute(_sql(template, dialect))
    for index in _INDEXES:
        op.execute(index)


def downgrade() -> None:
    for table in ("telephony_turns", "telephony_spans", "telephony_events", "telephony_calls"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
