"""Cron schedules, processed event triggers and morning-briefing settings."""

import sqlalchemy as sa
from sqlalchemy import Column, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from pincer.db.ids import new_id
from pincer.db.types import IsoText, Uuid7


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"
    __table_args__ = (
        Index(
            "idx_schedules_next_run",
            "next_run_at",
            sqlite_where=sa.text("enabled = 1"),
            postgresql_where=sa.text("enabled = 1"),
        ),
    )

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    pincer_user_id: str = Field(sa_column=Column(Text(), nullable=False))
    name: str = Field(sa_column=Column(Text(), nullable=False))
    cron_expr: str = Field(sa_column=Column(Text(), nullable=False))
    action: str = Field(sa_column=Column(Text(), nullable=False))
    channel: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'telegram'"))
    )
    timezone: str | None = Field(
        default=None, sa_column=Column(Text(), nullable=False, server_default=sa.text("'UTC'"))
    )
    enabled: int | None = Field(default=None, sa_column=Column(Integer(), nullable=False, server_default=sa.text("1")))
    last_run_at: str | None = Field(default=None, sa_column=Column(Text()))
    next_run_at: str | None = Field(default=None, sa_column=Column(Text()))
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
    updated_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )


class EventTrigger(SQLModel, table=True):
    __tablename__ = "event_triggers"
    __table_args__ = (UniqueConstraint("trigger_type", "trigger_key"),)

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    trigger_type: str = Field(sa_column=Column(Text(), nullable=False))
    trigger_key: str = Field(sa_column=Column(Text(), nullable=False))
    pincer_user_id: str = Field(sa_column=Column(Text(), nullable=False))
    processed_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
    result: str | None = Field(default=None, sa_column=Column(Text()))


class BriefingConfig(SQLModel, table=True):
    __tablename__ = "briefing_configs"

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    pincer_user_id: str = Field(sa_column=Column(Text(), unique=True, nullable=False))
    sections: str | None = Field(
        default=None,
        sa_column=Column(Text(), nullable=False, server_default=sa.text('\'["weather","calendar","email","news"]\'')),
    )
    custom_sections: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'[]'")))
    weather_location: str | None = Field(default=None, sa_column=Column(Text(), server_default=sa.text("'Berlin,DE'")))
    news_topics: str | None = Field(
        default=None, sa_column=Column(Text(), server_default=sa.text('\'["technology","business"]\''))
    )
    created_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
    updated_at: str | None = Field(
        default=None, sa_column=Column(IsoText(), server_default=sa.text("CURRENT_TIMESTAMP"))
    )
