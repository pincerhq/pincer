"""
LLM cost tracking with SQLite storage and budget enforcement.

Pricing is per 1M tokens, updated Feb 2026. Add new models as needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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

    async def get_today_spend(self) -> float:
        """Get total spend for today (UTC)."""
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
            return float(row[0]) if row else 0.0

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
