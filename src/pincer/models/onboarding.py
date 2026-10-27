"""First-conversation onboarding, one row per user and channel."""

import sqlalchemy as sa
from sqlalchemy import Column, Integer, Text
from sqlmodel import Field, SQLModel

from pincer.db.types import Real


class Onboarding(SQLModel, table=True):
    """Whether a user has been asked the onboarding question, and answered.

    `asked_session_id` is the session whose reply carried the question. The
    user's next message in any session is checked for an answer; `attempts`
    counts the ones that held none. Times are epoch seconds, as in
    `pincer_sessions`.
    """

    __tablename__ = "pincer_onboarding"

    user_id: str = Field(sa_column=Column(Text(), primary_key=True))
    channel: str = Field(sa_column=Column(Text(), primary_key=True))
    asked_session_id: str | None = Field(default=None, sa_column=Column(Text()))
    asked_at: float | None = Field(default=None, sa_column=Column(Real()))
    completed_at: float | None = Field(default=None, sa_column=Column(Real()))
    attempts: int = Field(default=0, sa_column=Column(Integer(), nullable=False, server_default=sa.text("0")))
