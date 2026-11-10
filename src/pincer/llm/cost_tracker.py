"""
LLM cost tracking with budget enforcement.

`CostTracker` is the compatibility façade over `pincer.services.costs.CostService`:
same constructor, same methods, same return shapes, so its callers (the agent,
the CLI, image generation) do not change. New code uses the service directly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pincer.db import ensure_schema_current
from pincer.db.engine import get_database_url
from pincer.exceptions import BudgetCheckFailedError, DailyBudgetReachedError
from pincer.llm.base import BaseLLMProvider
from pincer.services.costs import CostService, CostSummary

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.config.llm import ReasoningEffort, ThinkingMode
    from pincer.config.pricing import PriceTable, TokenUsage
    from pincer.llm.base import LLMMessage, LLMResponse

__all__ = ["CostSummary", "CostTracker", "MeteredLLM", "get_cost_tracker", "record_response"]


class CostTracker:
    """Cost tracker and the daily budget check, backed by `CostService`."""

    def __init__(self, db_path: Path, daily_budget: float = 0.0, prices: PriceTable | None = None) -> None:
        self._db_path = db_path
        self._daily_budget = daily_budget
        self._prices = prices
        self._service: CostService | None = None

    async def initialize(self) -> None:
        """Bring the database to head (see pincer.db.migrations) and attach the service."""
        await asyncio.to_thread(ensure_schema_current, self._db_path)
        self._service = CostService(get_database_url(self._db_path), self._daily_budget, self._prices)

    async def close(self) -> None:
        """Drop the service. The engine is shared, so the process disposes it
        at shutdown (`dispose_engines`), not each store that used it."""
        self._service = None

    @property
    def service(self) -> CostService:
        if self._service is None:
            raise RuntimeError("CostTracker not initialized")
        return self._service

    async def record(
        self,
        provider: str,
        model: str,
        usage: TokenUsage,
        session_id: str | None = None,
        is_free: bool = False,
    ) -> float:
        """Record a cost entry and return the cost.

        An unpriced model or a failed write is logged and returns without raising
        (see `CostService.record`); a tracker used before `initialize()` raises.
        """
        return await self.service.record(provider, model, usage, session_id, is_free)

    async def add_image_cost(self, cost_usd: float, provider: str, model: str = "") -> None:
        await self.service.add_image_cost(cost_usd, provider, model)

    async def get_image_count_today(self) -> int:
        return await self.service.get_image_count_today()

    async def get_today_spend(self) -> float:
        return await self.service.get_today_spend()

    async def budget_reached(self) -> bool:
        return await self.service.budget_reached()

    async def get_summary(self, since_timestamp: float | None = None) -> CostSummary:
        return await self.service.get_summary(since_timestamp)

    async def get_daily_costs(self, date_str: str) -> dict[str, Any]:
        return await self.service.get_daily_costs(date_str)

    async def get_daily_history(self, start: str, end: str) -> list[dict[str, Any]]:
        return await self.service.get_daily_history(start, end)

    async def get_costs_by_model(self, start: str, end: str) -> list[dict[str, Any]]:
        return await self.service.get_costs_by_model(start, end)

    async def get_costs_by_tool(self, start: str, end: str) -> list[dict[str, Any]]:
        return await self.service.get_costs_by_tool(start, end)

    async def get_budget_status(self) -> dict[str, Any]:
        return await self.service.get_budget_status()


async def record_response(
    costs: CostTracker,
    llm: BaseLLMProvider,
    response: LLMResponse,
    default_provider: str,
    session_id: str | None = None,
) -> float:
    """Record one completed request and return its cost.

    It is attributed to the provider that actually served it (a failover may
    differ from the configured primary), priced under the model name that was
    sent, and free when the router says that provider is (a keyless local endpoint).
    """
    from pincer.llm.router import LLMRouter

    provider = response.provider or default_provider
    return await costs.record(
        provider=provider,
        model=response.request_model or response.model,
        usage=response.usage,
        session_id=session_id,
        is_free=llm.is_free(provider) if isinstance(llm, LLMRouter) else False,
    )


class MeteredLLM(BaseLLMProvider):
    """A provider whose every request goes through the daily budget check first and is
    recorded after, for callers outside the agent's turn loops (a finished call's report).

    A request refused by the budget raises `DailyBudgetReachedError` before anything is
    sent, and one whose budget can't be read raises `BudgetCheckFailedError`; callers
    already treat a failed request as "no answer" and take their fallback.
    `stream_turn` is the base fallback over `complete`, so it is metered the same way.
    """

    def __init__(self, llm: BaseLLMProvider, costs: CostTracker, default_provider: str) -> None:
        self._llm = llm
        self._costs = costs
        self._default_provider = default_provider

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
        pinned: bool = False,
    ) -> LLMResponse:
        try:
            reached = await self._costs.budget_reached()
        except Exception as e:
            raise BudgetCheckFailedError("the daily budget could not be read: the request was not sent") from e
        if reached:
            raise DailyBudgetReachedError("daily budget reached: the request was not sent")
        response = await self._llm.complete(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            pinned=pinned,
        )
        await record_response(self._costs, self._llm, response, self._default_provider)
        return response

    async def close(self) -> None:
        """Nothing to close: the wrapped provider belongs to whoever built it."""


_cost_tracker: CostTracker | None = None


async def get_cost_tracker(db_path: Path | None = None, daily_budget: float = 5.0) -> CostTracker:
    """Singleton accessor for the cost tracker."""
    global _cost_tracker
    if _cost_tracker is None:
        if db_path is None:
            from pincer.config import get_settings

            settings = get_settings()
            db_path = settings.db_path
            daily_budget = settings.daily_budget_usd
        tracker = CostTracker(db_path, daily_budget)
        await tracker.initialize()
        _cost_tracker = tracker
    return _cost_tracker
