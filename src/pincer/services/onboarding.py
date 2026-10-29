"""Onboarding state: whether a user on a channel was asked, where, and whether they answered."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from pincer.db.session import session_scope
from pincer.repositories.onboarding import OnboardingRepository
from pincer.services.base import DatabaseService

# (database url, user id, channel) pairs known to be done with onboarding, so
# their turns read nothing. Done is final, short of a rename (`forget_user`).
_COMPLETED_CACHE_SIZE = 4096

_completed: OrderedDict[tuple[str | None, str, str], None] = OrderedDict()


def _remember_completed(url: str | None, user_id: str, channel: str) -> None:
    _completed[(url, user_id, channel)] = None
    _completed.move_to_end((url, user_id, channel))
    while len(_completed) > _COMPLETED_CACHE_SIZE:
        _completed.popitem(last=False)


def forget_user(url: str | None, user_id: str) -> None:
    """Drop a user's cached pairs (an identity rename moves or merges their rows).

    Only this process's cache: a rename made by another process reaches it
    when that process restarts.
    """
    for key in [key for key in _completed if key[0] == url and key[1] == user_id]:
        del _completed[key]


def _reset_for_tests() -> None:
    _completed.clear()


@dataclass(frozen=True, slots=True)
class OnboardingRecord:
    asked_session_id: str | None
    asked_at: float | None
    completed_at: float | None
    attempts: int


class OnboardingService(DatabaseService):
    def is_completed_cached(self, user_id: str, channel: str) -> bool:
        """True when this process already knows the user is done; no read."""
        key = (self._url, user_id, channel)
        if key not in _completed:
            return False
        _completed.move_to_end(key)
        return True

    async def get(self, user_id: str, channel: str) -> OnboardingRecord | None:
        async with session_scope(self._url) as session:
            row = await OnboardingRepository(session).get((user_id, channel))
            if row is None:
                return None
            if row.completed_at is not None:
                _remember_completed(self._url, user_id, channel)
            return OnboardingRecord(row.asked_session_id, row.asked_at, row.completed_at, row.attempts)

    async def claim_prompt(self, user_id: str, channel: str, session_id: str, *, now: float) -> bool:
        """Record that `session_id` asked the question. False when another turn
        already did."""
        async with session_scope(self._url) as session:
            return await OnboardingRepository(session).insert_if_absent(
                {"user_id": user_id, "channel": channel, "asked_session_id": session_id, "asked_at": now}
            )

    async def complete(self, user_id: str, channel: str, *, now: float) -> bool:
        """Close an open gate, for example when the answer window expires."""
        return await self._complete(user_id, channel, now=now, answer=False)

    async def complete_answer(self, user_id: str, channel: str, *, now: float) -> bool:
        """Claim an answer, including after an empty close; expiry and answers are final."""
        return await self._complete(user_id, channel, now=now, answer=True)

    async def _complete(self, user_id: str, channel: str, *, now: float, answer: bool) -> bool:
        async with session_scope(self._url) as session:
            won = await OnboardingRepository(session).mark_completed(user_id, channel, now, answer=answer)
        _remember_completed(self._url, user_id, channel)
        return won

    async def record_empty_reply(self, user_id: str, channel: str, *, max_empty: int, now: float) -> bool:
        """Count a reply with no answer in it; True when that closed the gate."""
        async with session_scope(self._url) as session:
            closed = await OnboardingRepository(session).count_empty_reply(
                user_id, channel, max_empty=max_empty, now=now
            )
        if closed:
            _remember_completed(self._url, user_id, channel)
        return closed
