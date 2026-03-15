"""
LLM cost tracking with SQLite storage and budget enforcement.

Pricing is per 1M tokens, updated Feb 2026. Add new models as needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.exceptions import BudgetExceededError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ── Pricing per 1M tokens (input, output) in USD ────────
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    # OpenAI
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.0, 30.0),
    "o1": (15.0, 60.0),
    "o1-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
}

DEFAULT_PRICING = (3.0, 15.0)


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a single API call."""
    input_rate, output_rate = PRICING.get(model, DEFAULT_PRICING)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


@dataclass
class CostSummary:
    total_usd: float
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int


class CostTracker:
    """Async SQLite-backed cost tracker with budget enforcement."""

    def __init__(self, db_path: Path, daily_budget: float = 0.0) -> None:
        self._db_path = db_path
        self._daily_budget = daily_budget
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open database and create tables."""
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                session_id TEXT
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cost_timestamp ON cost_log(timestamp)
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS image_cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                cost_usd REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_image_cost_timestamp ON image_cost_log(timestamp)
        """)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str | None = None,
    ) -> float:
        """Record a cost entry and return the cost. Raises BudgetExceededError if over limit."""
        assert self._db is not None, "CostTracker not initialized"

        cost = calculate_cost(model, input_tokens, output_tokens)

        if self._daily_budget > 0:
            today_spent = await self.get_today_spend()
            if today_spent + cost > self._daily_budget:
                raise BudgetExceededError(spent=today_spent + cost, limit=self._daily_budget)

        await self._db.execute(
            """INSERT INTO cost_log
               (timestamp, provider, model, input_tokens, output_tokens, cost_usd, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), provider, model, input_tokens, output_tokens, cost, session_id),
        )
        await self._db.commit()

        logger.debug(
            "Cost: $%.6f (%din/%dout) model=%s session=%s",
            cost,
            input_tokens,
            output_tokens,
            model,
            session_id,
        )
        return cost

    async def add_image_cost(self, cost_usd: float, provider: str, model: str = "") -> None:
        """Record an image generation cost entry."""
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO image_cost_log (timestamp, provider, model, cost_usd) VALUES (?, ?, ?, ?)",
            (time.time(), provider, model, cost_usd),
        )
        await self._db.commit()

    async def get_image_count_today(self) -> int:
        """Get the number of image generations today (UTC)."""
        assert self._db is not None
        today_start = (
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        async with self._db.execute(
            "SELECT COUNT(*) FROM image_cost_log WHERE timestamp >= ?",
            (today_start,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_today_spend(self) -> float:
        """Get total spend for today (UTC), including LLM and image costs."""
        assert self._db is not None
        today_start = (
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        async with self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_log WHERE timestamp >= ?",
            (today_start,),
        ) as cursor:
            row = await cursor.fetchone()
            llm_spend = float(row[0]) if row else 0.0

        async with self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM image_cost_log WHERE timestamp >= ?",
            (today_start,),
        ) as cursor:
            row = await cursor.fetchone()
            image_spend = float(row[0]) if row else 0.0

        return llm_spend + image_spend

    async def get_summary(self, since_timestamp: float | None = None) -> CostSummary:
        """Get aggregated cost summary."""
        assert self._db is not None
        query = (
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM cost_log"
        )
        params: tuple[float, ...] = ()
        if since_timestamp:
            query += " WHERE timestamp >= ?"
            params = (since_timestamp,)

        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                return CostSummary(
                    total_usd=float(row[0]),
                    total_calls=int(row[1]),
                    total_input_tokens=int(row[2]),
                    total_output_tokens=int(row[3]),
                )
            return CostSummary(0.0, 0, 0, 0)

    # ── Sprint 5: Extended query methods for API ─────────

    async def get_daily_costs(self, date_str: str) -> dict[str, Any]:
        """Get costs for a specific date (YYYY-MM-DD)."""
        assert self._db is not None
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        day_end = day_start + 86400

        async with self._db.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM cost_log "
            "WHERE timestamp >= ? AND timestamp < ?",
            (day_start, day_end),
        ) as cursor:
            row = await cursor.fetchone()
            total = float(row[0]) if row else 0.0
            count = int(row[1]) if row else 0

        by_model: dict[str, float] = {}
        async with self._db.execute(
            "SELECT model, COALESCE(SUM(cost_usd),0) FROM cost_log "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY model",
            (day_start, day_end),
        ) as cursor:
            async for row in cursor:
                by_model[row[0]] = round(float(row[1]), 6)

        by_tool: dict[str, float] = {}
        return {
            "total": round(total, 6),
            "request_count": count,
            "by_model": by_model,
            "by_tool": by_tool,
        }

    async def get_daily_history(
        self, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Get daily spend history between two dates (YYYY-MM-DD)."""
        assert self._db is not None
        start_ts = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        end_ts = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() + 86400

        async with self._db.execute(
            "SELECT date(timestamp, 'unixepoch') as day, "
            "COALESCE(SUM(cost_usd),0), COUNT(*) FROM cost_log "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY day ORDER BY day",
            (start_ts, end_ts),
        ) as cursor:
            return [
                {"date": row[0], "total": round(float(row[1]), 6), "requests": int(row[2])}
                async for row in cursor
            ]

    async def get_costs_by_model(
        self, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Get cost breakdown by model between two dates."""
        assert self._db is not None
        start_ts = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        end_ts = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() + 86400

        async with self._db.execute(
            "SELECT model, COALESCE(SUM(cost_usd),0), COUNT(*), "
            "COALESCE(SUM(input_tokens+output_tokens),0) FROM cost_log "
            "WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY model ORDER BY SUM(cost_usd) DESC",
            (start_ts, end_ts),
        ) as cursor:
            return [
                {
                    "model": row[0],
                    "total": round(float(row[1]), 6),
                    "requests": int(row[2]),
                    "tokens": int(row[3]),
                }
                async for row in cursor
            ]

    async def get_costs_by_tool(
        self, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Placeholder for per-tool cost breakdown (requires tool tracking)."""
        return []

    async def get_budget_status(self) -> dict[str, Any]:
        """Get current budget status."""
        today_spent = await self.get_today_spend()
        return {
            "daily_limit": self._daily_budget,
            "spent_pct": round(
                (today_spent / self._daily_budget) * 100, 1
            )
            if self._daily_budget > 0
            else 0,
            "remaining": round(
                max(0, self._daily_budget - today_spent), 4
            ),
            "is_downgraded": (
                today_spent / self._daily_budget >= 0.7
                if self._daily_budget > 0
                else False
            ),
        }


_cost_tracker: CostTracker | None = None


async def get_cost_tracker(
    db_path: Path | None = None, daily_budget: float = 5.0
) -> CostTracker:
    """Singleton accessor for the cost tracker."""
    global _cost_tracker
    if _cost_tracker is None:
        if db_path is None:
            from pincer.config import get_settings
            s = get_settings()
            db_path = s.db_path
            daily_budget = s.daily_budget_usd
        _cost_tracker = CostTracker(db_path, daily_budget)
        await _cost_tracker.initialize()
    return _cost_tracker
