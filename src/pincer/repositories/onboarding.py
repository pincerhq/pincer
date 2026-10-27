"""Onboarding state: `onboarding`, one row per user and channel."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.onboarding import Onboarding
from pincer.repositories.base import BaseRepository
from pincer.repositories.sessions import rekeyed_session_id

_TABLE = Onboarding.__table__  # type: ignore[attr-defined]


class OnboardingRepository(BaseRepository[Onboarding, tuple[str, str]]):
    model = Onboarding

    async def insert_if_absent(self, values: dict[str, Any]) -> bool:
        """Create the row; False when the user and channel already have one."""
        result = await self.session.exec(
            upsert(dialect_of(self.session), Onboarding, values, index_elements=["user_id", "channel"])
        )
        return int(result.rowcount) == 1

    async def mark_completed(self, user_id: str, channel: str, completed_at: float) -> bool:
        """Close the gate, creating the row for a user asked before it existed.

        True only for the call that closed it: a row already closed keeps the
        time it was closed, and this call changes nothing.
        """
        result = await self.session.exec(
            upsert(
                dialect_of(self.session),
                Onboarding,
                {"user_id": user_id, "channel": channel, "completed_at": completed_at},
                index_elements=["user_id", "channel"],
                set_=lambda excluded: {"completed_at": excluded.completed_at},
                where=_TABLE.c.completed_at.is_(None),
            )
        )
        return int(result.rowcount) == 1

    async def count_empty_reply(self, user_id: str, channel: str, *, max_empty: int, now: float) -> bool:
        """Count a reply that held no answer; the `max_empty`-th one closes the
        gate. True when the gate is closed afterwards."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                Onboarding,
                {
                    "user_id": user_id,
                    "channel": channel,
                    "attempts": 1,
                    "completed_at": now if max_empty <= 1 else None,
                },
                index_elements=["user_id", "channel"],
                set_={
                    "attempts": _TABLE.c.attempts + 1,
                    "completed_at": case((_TABLE.c.attempts + 1 >= max_empty, now), else_=None),
                },
                where=_TABLE.c.completed_at.is_(None),
            )
        )
        row = await self.session.exec(
            select(Onboarding.completed_at).where(
                col(Onboarding.user_id) == user_id, col(Onboarding.channel) == channel
            )
        )
        return row.one() is not None

    async def reassign_user(self, old_user_id: str, new_user_id: str) -> None:
        """Move a renamed identity's rows, and re-key their asking session as
        the session itself is (`rekeyed_session_id`).

        Where both ids have a row for the channel (a merge), a completed one
        wins; when both or neither are, the new id's row is kept.
        """
        source_done = select(Onboarding.channel).where(
            col(Onboarding.user_id) == old_user_id, col(Onboarding.completed_at).is_not(None)
        )
        await self.delete_where(
            col(Onboarding.user_id) == new_user_id,
            col(Onboarding.completed_at).is_(None),
            col(Onboarding.channel).in_(source_done),
        )
        taken = select(Onboarding.channel).where(col(Onboarding.user_id) == new_user_id)
        await self.delete_where(col(Onboarding.user_id) == old_user_id, col(Onboarding.channel).in_(taken))
        rows = await self.session.exec(
            select(Onboarding.channel, Onboarding.asked_session_id).where(col(Onboarding.user_id) == old_user_id)
        )
        for channel, asked in rows.all():
            moved = None if asked is None else rekeyed_session_id(asked, old_user_id, new_user_id)
            await self.session.exec(
                update(Onboarding)
                .where(col(Onboarding.user_id) == old_user_id, col(Onboarding.channel) == channel)
                .values(user_id=new_user_id, asked_session_id=moved or asked)
            )
