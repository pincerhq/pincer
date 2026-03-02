"""
Pincer Budget Enforcement — Hard limits with notifications and auto-downgrade.

Orchestration layer on top of CostTracker: tracks in-memory daily spend,
auto-downgrades to cheaper models near limits, sends user notifications.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

MODEL_COST_TIERS = [
    {"model": "claude-sonnet-4-5-20250929", "cost_per_1k": 0.009, "tier": "premium"},
    {"model": "claude-haiku-4-5-20251001", "cost_per_1k": 0.002, "tier": "standard"},
    {"model": "gpt-4o", "cost_per_1k": 0.0075, "tier": "premium"},
    {"model": "gpt-4o-mini", "cost_per_1k": 0.00045, "tier": "budget"},
]


@dataclass
class ConversationBudget:
    conversation_id: str
    spent_usd: float = 0.0
    limit_usd: float = 1.0
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


@dataclass
class BudgetStatus:
    daily_spent: float
    daily_limit: float
    daily_pct: float
    conversation_spent: float
    conversation_limit: float
    active_model: str
    is_downgraded: bool
    warning_sent: bool
    budget_exhausted: bool


class BudgetExhausted(Exception):
    def __init__(
        self,
        message: str,
        daily_spent: float = 0,
        daily_limit: float = 0,
    ) -> None:
        self.message = message
        self.daily_spent = daily_spent
        self.daily_limit = daily_limit
        super().__init__(message)


class BudgetEnforcer:
    """Budget enforcement with auto-downgrade and notification support."""

    def __init__(
        self,
        daily_limit_usd: float = 5.0,
        conversation_limit_usd: float = 1.0,
        warning_threshold_pct: float = 0.80,
        auto_downgrade_threshold_pct: float = 0.70,
        notify_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._daily_limit = daily_limit_usd
        self._conversation_limit = conversation_limit_usd
        self._warning_pct = warning_threshold_pct
        self._downgrade_pct = auto_downgrade_threshold_pct
        self.notify = notify_callback

        self._daily_spend: dict[str, float] = {}
        self._daily_date: str = ""
        self._conversations: dict[str, ConversationBudget] = {}
        self._warning_sent: dict[str, bool] = {}
        self._downgraded: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def check_budget(
        self,
        user_id: str,
        estimated_cost: float = 0.0,
        conversation_id: str | None = None,
    ) -> None:
        """Pre-flight budget check. Raises BudgetExhausted if over limit."""
        async with self._lock:
            self._reset_if_new_day()
            daily = self._daily_spend.get(user_id, 0.0)

            if daily + estimated_cost > self._daily_limit:
                if self.notify:
                    await self.notify(
                        user_id,
                        f"Daily budget exhausted "
                        f"(${daily:.2f}/${self._daily_limit:.2f}).\n"
                        f"Processing paused until midnight UTC.\n"
                        f"Say 'Increase my budget to $X' to continue.",
                    )
                raise BudgetExhausted(
                    "Daily limit reached",
                    daily_spent=daily,
                    daily_limit=self._daily_limit,
                )

            if conversation_id:
                conv = self._conversations.get(conversation_id)
                if (
                    conv
                    and conv.spent_usd + estimated_cost > conv.limit_usd
                ):
                    raise BudgetExhausted(
                        f"Conversation budget reached: "
                        f"${conv.spent_usd:.2f}/${conv.limit_usd:.2f}"
                    )

    async def record_cost(
        self,
        user_id: str,
        cost_usd: float,
        conversation_id: str | None = None,
    ) -> BudgetStatus:
        """Record a cost and return updated budget status."""
        async with self._lock:
            self._reset_if_new_day()
            self._daily_spend[user_id] = (
                self._daily_spend.get(user_id, 0.0) + cost_usd
            )
            daily = self._daily_spend[user_id]

            if conversation_id:
                if conversation_id not in self._conversations:
                    self._conversations[conversation_id] = ConversationBudget(
                        conversation_id=conversation_id,
                        limit_usd=self._conversation_limit,
                    )
                self._conversations[conversation_id].spent_usd += cost_usd

            pct = daily / self._daily_limit if self._daily_limit > 0 else 0

            if (
                pct >= self._warning_pct
                and not self._warning_sent.get(user_id)
            ):
                self._warning_sent[user_id] = True
                if self.notify:
                    await self.notify(
                        user_id,
                        f"{pct * 100:.0f}% of daily budget used "
                        f"(${daily:.2f}/${self._daily_limit:.2f}). "
                        f"Switching to efficient model.",
                    )

            is_downgraded = pct >= self._downgrade_pct
            self._downgraded[user_id] = is_downgraded

            conv = (
                self._conversations.get(conversation_id)
                if conversation_id
                else None
            )
            return BudgetStatus(
                daily_spent=daily,
                daily_limit=self._daily_limit,
                daily_pct=pct,
                conversation_spent=conv.spent_usd if conv else 0.0,
                conversation_limit=(
                    conv.limit_usd if conv else self._conversation_limit
                ),
                active_model="",
                is_downgraded=is_downgraded,
                warning_sent=self._warning_sent.get(user_id, False),
                budget_exhausted=pct >= 1.0,
            )

    async def get_model_for_budget(
        self, user_id: str, preferred_model: str
    ) -> str:
        """Return a cheaper model if user is near their budget limit."""
        async with self._lock:
            self._reset_if_new_day()
            if not self._downgraded.get(user_id):
                return preferred_model

            current_tier = next(
                (m for m in MODEL_COST_TIERS if m["model"] == preferred_model),
                None,
            )
            if not current_tier:
                return preferred_model

            provider = (
                "anthropic" if "claude" in preferred_model else "openai"
            )
            alternatives = [
                m
                for m in MODEL_COST_TIERS
                if ("claude" in m["model"]) == (provider == "anthropic")
                and m["cost_per_1k"] < current_tier["cost_per_1k"]
            ]
            if alternatives:
                return min(alternatives, key=lambda m: m["cost_per_1k"])[
                    "model"
                ]
            return preferred_model

    async def increase_budget(self, user_id: str, new_limit: float) -> str:
        """User-facing budget adjustment."""
        if new_limit <= 0 or new_limit > 100:
            return "Budget must be between $0.01 and $100."
        old = self._daily_limit
        self._daily_limit = new_limit
        self._warning_sent[user_id] = False
        self._downgraded[user_id] = False
        return (
            f"Daily budget increased from ${old:.2f} to ${new_limit:.2f}. "
            f"Back to premium model."
        )

    async def get_status(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            self._reset_if_new_day()
            daily = self._daily_spend.get(user_id, 0.0)
            return {
                "daily_spent_usd": round(daily, 4),
                "daily_limit_usd": self._daily_limit,
                "daily_pct": round(
                    (daily / self._daily_limit) * 100, 1
                )
                if self._daily_limit > 0
                else 0,
                "is_downgraded": self._downgraded.get(user_id, False),
            }

    def _reset_if_new_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_spend.clear()
            self._warning_sent.clear()
            self._downgraded.clear()
            self._daily_date = today
