"""Spend accounting and the daily budget.

Every method is one unit of work on the configured database. The service holds
no connection, only the database URL, so one instance serves any event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends

from pincer.db.engine import ensure_schema_current, get_database_url
from pincer.db.session import session_scope
from pincer.exceptions import BudgetExceededError
from pincer.models.costs import CostLog, ImageCostLog
from pincer.repositories.costs import CostLogRepository, ImageCostLogRepository

if TYPE_CHECKING:
    from pincer.config import Settings

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

_DAY_S = 86400


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


def _utc_today_start() -> float:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _day_start(date_str: str) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


class CostService:
    def __init__(self, url: str | None = None, daily_budget: float = 0.0) -> None:
        self._url = url
        self.daily_budget = daily_budget

    @classmethod
    async def from_settings(cls, settings: Settings | None = None) -> CostService:
        """The configured database (brought to head) and daily budget."""
        if settings is None:
            from pincer.config import get_settings

            settings = get_settings()
        await asyncio.to_thread(ensure_schema_current, settings.db_path)
        return cls(get_database_url(settings.db_path), settings.daily_budget_usd)

    # ── writes ───────────────────────────────────────────────────────

    async def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str | None = None,
        is_free: bool = False,
    ) -> float:
        """Record an LLM call and return its cost.

        Raises `BudgetExceededError`, recording nothing, when the call would
        take today's spend over the daily budget. `is_free` (e.g. a keyless
        local endpoint) forces zero cost.
        """
        cost = 0.0 if is_free else calculate_cost(model, input_tokens, output_tokens)
        # The check reads in its own transaction, deliberately, rather than
        # sharing one with the insert below: a SQLite transaction that reads
        # first and writes after fails outright with SQLITE_BUSY_SNAPSHOT if
        # another process committed in between, and `busy_timeout` does not
        # retry that one. The budget is advisory to a hair's breadth either
        # way; failing an LLM turn over it is not.
        if cost > 0 and self.daily_budget > 0:
            today_spent = await self.get_today_spend()
            if today_spent + cost > self.daily_budget:
                raise BudgetExceededError(spent=today_spent + cost, limit=self.daily_budget)

        async with session_scope(self._url) as session:
            await CostLogRepository(session).add(
                CostLog(
                    timestamp=time.time(),
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    session_id=session_id,
                ),
                refresh=False,
            )

        # Sprint 9 (T9.1): attribute this spend to the voice call currently
        # bound to the context, if any. `cost_logs.session_id` is per-user, not
        # per-call, so summing by session would bill a user's chat traffic to
        # whichever call happened to be running.
        try:
            from pincer.observability.call_costs import attribute_llm_cost

            attribute_llm_cost(input_tokens, output_tokens, cost)
        except Exception:  # pragma: no cover — accounting must never break a turn
            logger.debug("Per-call LLM cost attribution failed", exc_info=True)

        logger.debug(
            "Cost: $%.6f (%din/%dout) model=%s session=%s", cost, input_tokens, output_tokens, model, session_id
        )
        return cost

    async def add_image_cost(self, cost_usd: float, provider: str, model: str = "") -> None:
        async with session_scope(self._url) as session:
            await ImageCostLogRepository(session).add(
                ImageCostLog(timestamp=time.time(), provider=provider, model=model, cost_usd=cost_usd),
                refresh=False,
            )

    # ── reads ────────────────────────────────────────────────────────

    async def get_image_count_today(self) -> int:
        """Image generations today (UTC)."""
        async with session_scope(self._url) as session:
            return await ImageCostLogRepository(session).count_since(_utc_today_start())

    async def get_today_spend(self) -> float:
        """Today's spend (UTC), LLM and image generation together."""
        async with session_scope(self._url) as session:
            return await self._today_spend(session)

    async def get_summary(self, since_timestamp: float | None = None) -> CostSummary:
        async with session_scope(self._url) as session:
            # A falsy `since_timestamp` (None or 0) means all time, as before.
            spend, calls, tokens_in, tokens_out = await CostLogRepository(session).totals(since_timestamp or None)
        return CostSummary(
            total_usd=spend, total_calls=calls, total_input_tokens=tokens_in, total_output_tokens=tokens_out
        )

    async def get_daily_costs(self, date_str: str) -> dict[str, Any]:
        """LLM spend on one UTC day (`YYYY-MM-DD`)."""
        start = _day_start(date_str)
        async with session_scope(self._url) as session:
            repo = CostLogRepository(session)
            total, count = await repo.spend_between(start, start + _DAY_S)
            by_model = await repo.by_model(start, start + _DAY_S)
        return {
            "total": round(total, 6),
            "request_count": count,
            "by_model": {model: round(spend, 6) for model, spend, _, _ in by_model},
            "by_tool": {},
        }

    async def get_daily_history(self, start: str, end: str) -> list[dict[str, Any]]:
        """LLM spend per UTC day from `start` through `end` (both `YYYY-MM-DD`, inclusive)."""
        async with session_scope(self._url) as session:
            days = await CostLogRepository(session).by_day(_day_start(start), _day_start(end) + _DAY_S)
        return [{"date": day, "total": round(spend, 6), "requests": calls} for day, spend, calls in days]

    async def get_costs_by_model(self, start: str, end: str) -> list[dict[str, Any]]:
        """LLM spend per model from `start` through `end` (inclusive), most expensive first."""
        async with session_scope(self._url) as session:
            rows = await CostLogRepository(session).by_model(_day_start(start), _day_start(end) + _DAY_S)
        return [
            {"model": model, "total": round(spend, 6), "requests": calls, "tokens": tokens}
            for model, spend, calls, tokens in rows
        ]

    async def get_costs_by_tool(self, start: str, end: str) -> list[dict[str, Any]]:
        """Placeholder for per-tool cost breakdown (requires tool tracking)."""
        return []

    async def get_budget_status(self) -> dict[str, Any]:
        today_spent = await self.get_today_spend()
        budget = self.daily_budget
        return {
            "daily_limit": budget,
            "spent_today": round(today_spent, 6),
            "spent_pct": round((today_spent / budget) * 100, 1) if budget > 0 else 0,
            "remaining": round(max(0, budget - today_spent), 4),
            "is_downgraded": (today_spent / budget >= 0.7 if budget > 0 else False),
        }

    @staticmethod
    async def _today_spend(session: Any) -> float:
        start = _utc_today_start()
        return await CostLogRepository(session).spend_since(start) + await ImageCostLogRepository(session).spend_since(
            start
        )


async def get_cost_service() -> CostService:
    """FastAPI dependency: the configured database and daily budget."""
    return await CostService.from_settings()


CostServiceDep = Annotated[CostService, Depends(get_cost_service)]
