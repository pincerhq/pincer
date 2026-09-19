"""Voice observability: appointment outcomes, per-call costs and canary runs."""

import sqlalchemy as sa
from sqlalchemy import Column, Index, Integer, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import IsoText, Real


class AppointmentOutcome(SQLModel, table=True):
    __tablename__ = "appointment_outcomes"
    __table_args__ = (
        Index("idx_appointment_outcomes_recorded", "recorded_at"),
        Index("idx_appointment_outcomes_task", "task_id", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    task_id: str = Field(sa_column=Column(Text(), nullable=False))
    call_sid: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    result: str = Field(sa_column=Column(Text(), nullable=False))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    attempts: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    detail: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    recorded_at: str = Field(sa_column=Column(IsoText(), nullable=False))


class CallCost(SQLModel, table=True):
    __tablename__ = "call_costs"
    __table_args__ = (Index("idx_call_costs_recorded", "recorded_at"),)

    call_sid: str = Field(sa_column=Column(Text(), primary_key=True))
    direction: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    engine: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    language: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    duration_seconds: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    twilio_usd: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    stt_seconds: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    stt_usd: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    tts_characters: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    tts_usd: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    llm_input_tokens: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    llm_output_tokens: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    llm_usd: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    total_usd: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
    recorded_at: str = Field(sa_column=Column(IsoText(), nullable=False))


class CanaryRun(SQLModel, table=True):
    __tablename__ = "canary_runs"
    __table_args__ = (
        Index("idx_canary_runs_ran_at", "ran_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    ran_at: str = Field(sa_column=Column(IsoText(), nullable=False))
    ok: int = Field(sa_column=Column(Integer(), nullable=False))
    skipped: int | None = Field(default=None, sa_column=Column(Integer(), nullable=False, server_default=sa.text("0")))
    reason: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    call_sid: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("''")))
    turns: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("0")))
    duration_s: float | None = Field(default=None, sa_column=Column(Real(), server_default=sa.text("0.0")))
