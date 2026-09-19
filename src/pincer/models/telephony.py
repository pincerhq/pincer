"""Telephony telemetry: per-call rows, the event timeline, spans and per-turn latencies."""

import sqlalchemy as sa
from sqlalchemy import REAL, Column, Index, Integer, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import IsoText


class TelephonyCall(SQLModel, table=True):
    __tablename__ = "telephony_calls"
    __table_args__ = (
        Index("idx_tel_calls_provider_sid", "provider_call_id"),
        Index("idx_tel_calls_started", "registered_at"),
        Index("idx_tel_calls_status", "status"),
        Index("idx_tel_calls_tenant", "tenant_id"),
    )

    call_id: str = Field(sa_column=Column(Text(), primary_key=True))
    provider_call_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    trace_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    direction: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    provider: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    engine: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    transport: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    codec: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    sample_rate: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    model: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    tenant_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    environment: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    app_version: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    from_number_masked: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    to_number_masked: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    registered_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    dialed_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    answered_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    media_open_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    ended_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    status: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'active'")))
    outcome: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    failure_category: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    termination_reason: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    failure_code: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    duration_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    setup_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    media_establish_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    turn_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    tool_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    error_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    timeout_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    retry_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    interruption_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    reconnect_count: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    sampled: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    sample_rate_used: float | None = Field(default=None, sa_column=Column(REAL(), server_default=sa.text("1.0")))
    coverage: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'full'")))
    config_json: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'{}'")))
    updated_at: str | None = Field(default=None, sa_column=Column(IsoText()))


class TelephonyEvent(SQLModel, table=True):
    __tablename__ = "telephony_events"
    __table_args__ = (
        Index("idx_tel_events_call", "call_id", "ts_utc", "seq"),
        Index("idx_tel_events_name", "name"),
        Index("idx_tel_events_turn", "turn_id"),
    )

    event_id: str = Field(sa_column=Column(Text(), primary_key=True))
    call_id: str = Field(sa_column=Column(Text(), nullable=False))
    provider_call_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    trace_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    span_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    turn_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    name: str = Field(sa_column=Column(Text(), nullable=False))
    ts_utc: str = Field(sa_column=Column(IsoText(), nullable=False))
    mono_ns: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    seq: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    attributes: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'{}'")))


class TelephonySpan(SQLModel, table=True):
    __tablename__ = "telephony_spans"
    __table_args__ = (
        Index("idx_tel_spans_call", "call_id", "start_utc"),
        Index("idx_tel_spans_turn", "turn_id"),
    )

    span_id: str = Field(sa_column=Column(Text(), primary_key=True))
    call_id: str = Field(sa_column=Column(Text(), nullable=False))
    trace_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    parent_span_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    turn_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    name: str = Field(sa_column=Column(Text(), nullable=False))
    start_utc: str = Field(sa_column=Column(IsoText(), nullable=False))
    end_utc: str | None = Field(default=None, sa_column=Column(IsoText()))
    start_mono_ns: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    end_mono_ns: int | None = Field(default=None, sa_column=Column(Integer()))
    duration_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    status: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'ok'")))
    attempt: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    attributes: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'{}'")))


class TelephonyTurn(SQLModel, table=True):
    __tablename__ = "telephony_turns"
    __table_args__ = (
        Index("idx_tel_turns_call", "call_id", "turn_no"),
        Index("idx_tel_turns_created", "created_at"),
        Index("idx_tel_turns_latency", "response_latency_ms"),
    )

    turn_id: str = Field(sa_column=Column(Text(), primary_key=True))
    call_id: str = Field(sa_column=Column(Text(), nullable=False))
    turn_no: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    trigger: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    started_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    first_audio_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    engine: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    model: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    streamed: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    response_latency_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    response_latency_source: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    endpointing_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    stt_first_partial_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    stt_final_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    agent_queue_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    agent_prep_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    llm_ttft_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    llm_total_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    tool_total_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    tts_first_audio_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    tts_total_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    audio_queue_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    total_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    tool_calls: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    tool_retries: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    tool_timeouts: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    interrupted: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    cancelled: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    error: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    bottleneck_stage: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    bottleneck_ms: float | None = Field(default=None, sa_column=Column(REAL()))
    critical_path: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'[]'")))
    complete: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    created_at: str = Field(sa_column=Column(IsoText(), nullable=False))
