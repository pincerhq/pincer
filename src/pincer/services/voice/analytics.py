"""Per-call conversation analytics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.session import session_scope
from pincer.repositories.voice import AnalyticsRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class AnalyticsService(DatabaseService):
    async def save(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await AnalyticsRepository(session).save(values)

    async def get(self, call_sid: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await AnalyticsRepository(session).get(call_sid)
        return None if row is None else _as_dict(row)

    async def for_calls(self, call_sids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not call_sids:
            return {}
        async with session_scope(self._url) as session:
            rows = await AnalyticsRepository(session).for_calls(call_sids)
        return {str(row.call_sid): _as_dict(row) for row in rows}

    async def sentiment_counts(self, cutoff: str, *, direction: str | None = None) -> dict[str, int]:
        async with session_scope(self._url) as session:
            return await AnalyticsRepository(session).sentiment_counts(cutoff, direction=direction)

    async def negative_since(self, cutoff: str) -> int:
        async with session_scope(self._url) as session:
            return await AnalyticsRepository(session).negative_since(cutoff)


def _as_dict(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in row.__class__.model_fields}
