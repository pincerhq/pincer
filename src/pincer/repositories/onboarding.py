"""Onboarding state: `onboarding`, one row per user and channel."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.onboarding import Onboarding
from pincer.repositories.base import BaseRepository


class OnboardingRepository(BaseRepository[Onboarding, tuple[str, str]]):
    model = Onboarding

    async def insert_if_absent(self, values: dict[str, Any]) -> bool:
        """Create the row; False when the user and channel already have one."""
        result = await self.session.exec(
            upsert(dialect_of(self.session), Onboarding, values, index_elements=["user_id", "channel"])
        )
        return int(result.rowcount) == 1

    async def mark_completed(self, user_id: str, channel: str, completed_at: float) -> None:
        """Record the answer, creating the row for a user asked before it existed."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                Onboarding,
                {"user_id": user_id, "channel": channel, "completed_at": completed_at},
                index_elements=["user_id", "channel"],
                set_=lambda excluded: {"completed_at": excluded.completed_at},
            )
        )

    async def reassign_user(self, old_user_id: str, new_user_id: str) -> None:
        """Move a renamed identity's rows, and the id inside their asking session.

        Where the new id already has a row for the channel (a merge), that row
        is kept and the old one dropped.
        """
        taken = select(Onboarding.channel).where(col(Onboarding.user_id) == new_user_id)
        await self.delete_where(col(Onboarding.user_id) == old_user_id, col(Onboarding.channel).in_(taken))
        await self.session.exec(
            update(Onboarding)
            .where(col(Onboarding.user_id) == old_user_id)
            .values(
                user_id=new_user_id,
                asked_session_id=func.replace(col(Onboarding.asked_session_id), old_user_id, new_user_id),
            )
        )
