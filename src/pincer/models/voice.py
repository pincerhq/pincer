"""Phone calls: the call log, transcripts, actions, contacts, the dialling gate, messages, threads and analytics."""

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import IsoText, Real


class VoiceCall(SQLModel, table=True):
    __tablename__ = "voice_calls"
    __table_args__ = (
        Index("idx_calls_thread", "thread_id"),
        Index("idx_voice_calls_failure", "failure_code"),
        Index("idx_voice_calls_started", "started_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    call_sid: str = Field(sa_column=Column(Text(), unique=True, nullable=False))
    direction: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'inbound'"))
    )
    from_number: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    to_number: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    pincer_user_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    recording_enabled: bool | None = Field(default=None, sa_column=Column(Boolean(), server_default=sa.text("FALSE")))
    consent_given: bool | None = Field(default=None, sa_column=Column(Boolean(), server_default=sa.text("FALSE")))
    started_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    ended_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    failure_code: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    engine: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    report_delivered_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    inbound_intent: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    thread_id: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    thread_attach_kind: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    briefing_json: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))


class CallTranscript(SQLModel, table=True):
    __tablename__ = "call_transcripts"
    __table_args__ = (
        Index("idx_call_transcripts_call", "call_id"),
        Index("idx_call_transcripts_ts", "timestamp"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    call_id: str = Field(sa_column=Column(Text(), nullable=False))
    speaker: str = Field(sa_column=Column(Text(), nullable=False))
    text: str = Field(sa_column=Column(Text(), nullable=False))
    confidence: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("1.0")))
    is_final: bool | None = Field(default=None, sa_column=Column(Boolean(), server_default=sa.text("TRUE")))
    state: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    timestamp: str = Field(sa_column=Column(IsoText(), nullable=False))


class CallAction(SQLModel, table=True):
    __tablename__ = "call_actions"
    __table_args__ = (
        Index("idx_call_actions_call", "call_id"),
        Index("idx_call_actions_ts", "timestamp"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    call_id: str = Field(sa_column=Column(Text(), nullable=False))
    action_type: str = Field(sa_column=Column(Text(), nullable=False))
    tool_name: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    input_summary: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    output_summary: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    user_confirmed: bool | None = Field(default=None, sa_column=Column(Boolean()))
    timestamp: str = Field(sa_column=Column(IsoText(), nullable=False))
    tier: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    approval_mode: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    deny_reason: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))


class PhoneContact(SQLModel, table=True):
    __tablename__ = "phone_contacts"
    __table_args__ = (
        Index("idx_phone_contacts_user", "user_id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    user_id: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("''")))
    name: str = Field(sa_column=Column(Text(), nullable=False))
    phone_number: str = Field(sa_column=Column(Text(), nullable=False))
    category: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    ivr_tree_json: str | None = Field(default=None, sa_column=Column(Text()))
    notes: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )


class DoNotCallNumber(SQLModel, table=True):
    __tablename__ = "do_not_call_numbers"

    phone_number: str = Field(sa_column=Column(Text(), primary_key=True))
    reason: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    source: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    call_sid: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    added_at: str = Field(sa_column=Column(IsoText(), nullable=False))


class OutboundCallLog(SQLModel, table=True):
    __tablename__ = "outbound_call_logs"
    __table_args__ = (
        Index("idx_outbound_log_day", "local_day"),
        Index("idx_outbound_log_number", "phone_number", "placed_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    phone_number: str = Field(sa_column=Column(Text(), nullable=False))
    user_id: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("''")))
    channel: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    call_sid: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    placed_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    local_day: str = Field(sa_column=Column(Text(), nullable=False))


class InboundMessage(SQLModel, table=True):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        Index("idx_inbound_messages_call", "call_sid"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    call_sid: str = Field(sa_column=Column(Text(), nullable=False))
    caller_name: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    caller_name_unverified: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    callback_number: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    callback_unverified: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    matter: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    urgent: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    created_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    delivered_to_owner_at: str | None = Field(default=None, sa_column=Column(IsoText()))


class CallThread(SQLModel, table=True):
    __tablename__ = "call_threads"
    __table_args__ = (Index("idx_threads_number_status", "primary_number", "status"),)

    thread_id: str = Field(sa_column=Column(Text(), primary_key=True))
    subject: str = Field(sa_column=Column(Text(), nullable=False))
    status: str | None = Field(default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'open'")))
    origin: str = Field(sa_column=Column(Text(), nullable=False))
    primary_number: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    contact_name: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    rolling_summary: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    open_commitments: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'[]'")))
    created_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    updated_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    resolved_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    closed_at: str | None = Field(default=None, sa_column=Column(IsoText()))


class CallThreadMember(SQLModel, table=True):
    __tablename__ = "call_thread_members"
    __table_args__ = (Index("idx_thread_members_thread", "thread_id"),)

    call_sid: str = Field(sa_column=Column(Text(), primary_key=True))
    thread_id: str = Field(sa_column=Column(Text(), ForeignKey("call_threads.thread_id"), nullable=False))
    attach_kind: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("''"))
    )
    attached_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    call_started_at: str | None = Field(default=None, sa_column=Column(IsoText()))
    direction: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    outcome_code: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    task_result: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))


class CallAnalytics(SQLModel, table=True):
    __tablename__ = "call_analytics"
    __table_args__ = (Index("idx_call_analytics_sentiment", "sentiment"),)

    call_sid: str = Field(sa_column=Column(Text(), ForeignKey("voice_calls.call_sid"), primary_key=True))
    agent_speech_ms: int | None = Field(default=None, sa_column=Column(Integer()))
    caller_speech_ms: int | None = Field(default=None, sa_column=Column(Integer()))
    silence_ms: int | None = Field(default=None, sa_column=Column(Integer()))
    overlap_ms: int | None = Field(default=None, sa_column=Column(Integer()))
    interruptions: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    talk_ratio: float | None = Field(default=None, sa_column=Column(Real()))
    method: str = Field(sa_column=Column(Text(), nullable=False))
    sentiment: str | None = Field(default=None, sa_column=Column(Text()))
    sentiment_trajectory: str | None = Field(default=None, sa_column=Column(Text()))
    sentiment_rationale: str | None = Field(default=None, sa_column=Column(Text()))
    sentiment_reason: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    created_at: str = Field(sa_column=Column(IsoText(), nullable=False))
