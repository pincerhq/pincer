"""The structured audit log of every action the agent takes."""

import sqlalchemy as sa
from sqlalchemy import REAL, Column, Index, Integer, Text
from sqlmodel import Field, SQLModel


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_action", "action"),
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_tool", "tool"),
        Index("idx_audit_user", "user_id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    timestamp: str = Field(sa_column=Column(Text(), nullable=False))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    session_id: str | None = Field(default=None, sa_column=Column(Text()))
    action: str = Field(sa_column=Column(Text(), nullable=False))
    tool: str | None = Field(default=None, sa_column=Column(Text()))
    input_summary: str | None = Field(default=None, sa_column=Column(Text()))
    output_summary: str | None = Field(default=None, sa_column=Column(Text()))
    approved: int | None = Field(default=None, sa_column=Column(Integer(), server_default=sa.text("1")))
    cost_usd: float | None = Field(default=None, sa_column=Column(REAL(), server_default=sa.text("0.0")))
    duration_ms: int | None = Field(default=None, sa_column=Column(Integer()))
    ip_address: str | None = Field(default=None, sa_column=Column(Text()))
    channel: str | None = Field(default=None, sa_column=Column(Text()))
    metadata_json: str | None = Field(default=None, sa_column=Column(Text()))
