"""Per-call conversation analytics: `call_analytics`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func
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
