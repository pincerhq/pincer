"""Per-call conversation analytics: `call_analytics`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.voice import CallAnalytics, VoiceCall
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class AnalyticsRepository(BaseRepository[CallAnalytics, str]):
    model = CallAnalytics

    async def save(self, values: dict[str, Any]) -> None:
        """One analysis per call; re-analysing replaces it."""
        stored = dict(values)
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                CallAnalytics,
                stored,
                index_elements=["call_sid"],
                set_=lambda excluded: {name: excluded[name] for name in stored if name != "call_sid"},
            )
        )

    async def redact_rationales_older_than(self, cutoff: str) -> int:
        """Blank sentiment rationales for calls older than the cutoff.

        The `voice_calls` rows are usually already gone by the time retention
        runs, so the analytics row's own timestamp is the primary test; the
        subquery covers a call row that is still present but already past the
        cutoff.
        """
        expired_calls = select(col(VoiceCall.call_sid)).where(col(VoiceCall.started_at) < cutoff)
        stmt = (
            update(CallAnalytics)
            .where(
                col(CallAnalytics.sentiment_rationale).isnot(None),
                or_(col(CallAnalytics.created_at) < cutoff, col(CallAnalytics.call_sid).in_(expired_calls)),
            )
            .values(sentiment_rationale=None)
        )
        return int((await self.session.exec(stmt)).rowcount or 0)

    async def for_calls(self, call_sids: Sequence[str]) -> Sequence[CallAnalytics]:
        """A page of calls in one query, not one query per row."""
        return await self.list(col(CallAnalytics.call_sid).in_(list(call_sids)))

    async def sentiment_counts(self, cutoff: str, *, direction: str | None = None) -> dict[str, int]:
        """`{sentiment: calls}` for calls started since `cutoff`."""
        where = [col(CallAnalytics.sentiment).isnot(None), col(VoiceCall.started_at) >= cutoff]
        if direction:
            where.append(col(VoiceCall.direction) == direction)
        stmt = (
            select(CallAnalytics.sentiment, func.count())
            .join(VoiceCall, col(VoiceCall.call_sid) == col(CallAnalytics.call_sid))
            .where(*where)
            .group_by(col(CallAnalytics.sentiment))
        )
        return {str(sentiment): int(count) for sentiment, count in (await self.session.exec(stmt)).all()}

    async def negative_since(self, cutoff: str) -> int:
        stmt = (
            select(func.count())
            .select_from(CallAnalytics)
            .join(VoiceCall, col(VoiceCall.call_sid) == col(CallAnalytics.call_sid))
            .where(col(CallAnalytics.sentiment) == "negative", col(VoiceCall.started_at) >= cutoff)
        )
        return int((await self.session.exec(stmt)).one())
