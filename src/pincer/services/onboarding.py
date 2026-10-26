"""Onboarding state: whether a user on a channel was asked, where, and whether they answered."""

from __future__ import annotations

from dataclasses import dataclass

from pincer.db.session import session_scope
from pincer.repositories.onboarding import OnboardingRepository
from pincer.services.base import DatabaseService


@dataclass(frozen=True, slots=True)
class OnboardingRecord:
    asked_session_id: str | None
    asked_at: float | None
    completed_at: float | None


class OnboardingService(DatabaseService):
    async def get(self, user_id: str, channel: str) -> OnboardingRecord | None:
        async with session_scope(self._url) as session:
            row = await OnboardingRepository(session).get((user_id, channel))
            if row is None:
                return None
            return OnboardingRecord(row.asked_session_id, row.asked_at, row.completed_at)

    async def claim_prompt(self, user_id: str, channel: str, session_id: str, *, now: float) -> bool:
        """Record that `session_id` asks the question. False when another turn
        already did, so two first messages at once ask only once."""
        async with session_scope(self._url) as session:
            return await OnboardingRepository(session).insert_if_absent(
                {"user_id": user_id, "channel": channel, "asked_session_id": session_id, "asked_at": now}
            )

    async def complete(self, user_id: str, channel: str, *, now: float) -> None:
        async with session_scope(self._url) as session:
            await OnboardingRepository(session).mark_completed(user_id, channel, now)
