"""LLM and image-generation spend: `cost_logs` and `image_cost_logs`.

Timestamps in both tables are epoch seconds (REAL), so every range here is a
half-open `[start, end)` over floats.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import func
from sqlmodel import col, select

from pincer.db.dialect import day_bucket, dialect_of
from pincer.models.costs import CostLog, ImageCostLog
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


_TOKEN_COLUMNS = (
    CostLog.input_tokens,
    CostLog.output_tokens,
    CostLog.cache_read_tokens,
    CostLog.cache_write_5m_tokens,
    CostLog.cache_write_1h_tokens,
)


class ModelSpend(NamedTuple):
    model: str
    spend: float
    calls: int
    input_tokens: int  # uncached input
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int


class CostLogRepository(BaseRepository[CostLog, str]):
    model = CostLog

    async def spend_since(self, start: float) -> float:
        stmt = select(func.coalesce(func.sum(CostLog.cost_usd), 0.0)).where(col(CostLog.timestamp) >= start)
        return float((await self.session.exec(stmt)).one())

    async def totals(self, start: float | None = None) -> tuple[float, int, int, int, int, int, int]:
        """(spend, calls, input, output, cache read, 5-minute write, 1-hour write tokens),
        all time or from `start`. Input is uncached input."""
        stmt = select(
            func.coalesce(func.sum(CostLog.cost_usd), 0.0),
            func.count(),
            *(func.coalesce(func.sum(column), 0) for column in _TOKEN_COLUMNS),
        )
        if start is not None:
            stmt = stmt.where(col(CostLog.timestamp) >= start)
        row: tuple[Any, ...] = tuple((await self.session.exec(stmt)).one())
        tokens_in, tokens_out, read, write_5m, write_1h = (int(t) for t in row[2:])
        return float(row[0]), int(row[1]), tokens_in, tokens_out, read, write_5m, write_1h

    async def spend_between(self, start: float, end: float) -> tuple[float, int]:
        """(spend, calls) in `[start, end)`."""
        stmt = select(func.coalesce(func.sum(CostLog.cost_usd), 0.0), func.count()).where(*self._between(start, end))
        spend, calls = (await self.session.exec(stmt)).one()
        return float(spend), int(calls)

    async def by_model(self, start: float, end: float) -> list[ModelSpend]:
        """Spend and tokens per model in `[start, end)`, most expensive first."""
        spend = func.coalesce(func.sum(CostLog.cost_usd), 0.0)
        stmt = (
            select(
                CostLog.model,
                spend,
                func.count(),
                *(func.coalesce(func.sum(column), 0) for column in _TOKEN_COLUMNS),
            )
            .where(*self._between(start, end))
            .group_by(col(CostLog.model))
            .order_by(spend.desc())
        )
        return [
            ModelSpend(row[0], float(row[1]), *(int(v) for v in row[2:]))
            for row in (await self.session.exec(stmt)).all()
        ]

    async def by_day(self, start: float, end: float) -> Sequence[tuple[str, float, int]]:
        """(UTC day `YYYY-MM-DD`, spend, calls) in `[start, end)`, oldest first."""
        day = day_bucket(dialect_of(self.session), col(CostLog.timestamp)).label("day")
        stmt = (
            select(day, func.coalesce(func.sum(CostLog.cost_usd), 0.0), func.count())
            .where(*self._between(start, end))
            .group_by(day)
            .order_by(day)
        )
        return [(d, float(s), int(n)) for d, s, n in (await self.session.exec(stmt)).all()]

    @staticmethod
    def _between(start: float, end: float) -> tuple[Any, Any]:
        return col(CostLog.timestamp) >= start, col(CostLog.timestamp) < end


class ImageCostLogRepository(BaseRepository[ImageCostLog, str]):
    model = ImageCostLog

    async def spend_since(self, start: float) -> float:
        stmt = select(func.coalesce(func.sum(ImageCostLog.cost_usd), 0.0)).where(col(ImageCostLog.timestamp) >= start)
        return float((await self.session.exec(stmt)).one())

    async def count_since(self, start: float) -> int:
        stmt = select(func.count()).where(col(ImageCostLog.timestamp) >= start)
        return int((await self.session.exec(stmt)).one())
