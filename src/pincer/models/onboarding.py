"""First-conversation onboarding, one row per user and channel."""

from typing import Literal

import sqlalchemy as sa
from sqlalchemy import Column, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from pincer.db.ids import new_id
from pincer.db.types import Real, Uuid7
from pincer.models.identity import profile_ref

# Why a row's gate was closed: an answer, the answer window expiring, too many
# replies with no answer, or a user never asked (onboarded before the table
# existed, or with history from before onboarding).
ClosedBy = Literal["answer", "expired", "empty", "legacy", "existing"]


class Onboarding(SQLModel, table=True):
    """Whether a user has been asked the onboarding question, and answered.

    `asked_session_id` is the session whose reply carried the question. The
    user's next message in any session is checked for an answer; `attempts`
    counts the ones that held none, and after the answer window also the
    reads whose extraction call failed. Times are epoch seconds, as in
    `pincer_sessions`.
    """

    __tablename__ = "pincer_onboarding"
    __table_args__ = (UniqueConstraint("user_id", "channel", name="uq_onboarding_user_id_channel"),)

    id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
    user_id: str = Field(sa_column=Column(Text(), nullable=False))
    # Moved on a rename by its own repository (`move_user`, `reassign_user`), not `follow_profile`.
    profile_id: str | None = profile_ref("user_id", follows=False)
    channel: str = Field(sa_column=Column(Text(), nullable=False))
    asked_session_id: str | None = Field(default=None, sa_column=Column(Text()))
    asked_at: float | None = Field(default=None, sa_column=Column(Real()))
    completed_at: float | None = Field(default=None, sa_column=Column(Real()))
    closed_by: ClosedBy | None = Field(default=None, sa_column=Column(Text()))
    attempts: int = Field(default=0, sa_column=Column(Integer(), nullable=False, server_default=sa.text("0")))
