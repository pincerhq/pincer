"""
Pincer Rate Limiter — Token-bucket per-user + global rate limiting.

- Per-user: messages/min, tool calls/min (token bucket)
- Global: concurrent LLM calls (semaphore), daily API spend (budget check)
- All async-safe, zero external dependencies
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pincer.exceptions import RateLimitExceeded


@dataclass
class TokenBucket:
    """Async-safe token bucket rate limiter."""

    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    async def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Try to consume tokens. Returns (allowed, wait_seconds)."""
        async with self._lock:
            self._refill()
            if self.tokens >= amount:
                self.tokens -= amount
                return True, 0.0
            deficit = amount - self.tokens
            wait_time = deficit / self.refill_rate
            return False, wait_time

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now


class RateLimiter:
    """Per-user + global rate limiter for the Pincer agent."""

    def __init__(
        self,
        messages_per_minute: int = 30,
        tool_calls_per_minute: int = 20,
        max_concurrent_llm: int = 5,
        max_daily_spend_usd: float = 5.0,
    ) -> None:
        self._messages_per_minute = messages_per_minute
        self._tool_calls_per_minute = tool_calls_per_minute
        self._max_daily_spend_usd = max_daily_spend_usd
        self._user_message_buckets: dict[str, TokenBucket] = {}
        self._user_tool_buckets: dict[str, TokenBucket] = {}
        self._llm_semaphore = asyncio.Semaphore(max_concurrent_llm)
        self._daily_spend: float = 0.0
        self._daily_spend_lock = asyncio.Lock()
        self._spend_reset_date: str = ""

    def _get_message_bucket(self, user_id: str) -> TokenBucket:
        if user_id not in self._user_message_buckets:
            rpm = self._messages_per_minute
            self._user_message_buckets[user_id] = TokenBucket(
                capacity=float(rpm),
                refill_rate=rpm / 60.0,
            )
        return self._user_message_buckets[user_id]

    def _get_tool_bucket(self, user_id: str) -> TokenBucket:
        if user_id not in self._user_tool_buckets:
            tpm = self._tool_calls_per_minute
            self._user_tool_buckets[user_id] = TokenBucket(
                capacity=float(tpm),
                refill_rate=tpm / 60.0,
            )
        return self._user_tool_buckets[user_id]

    async def check_message(self, user_id: str) -> None:
        """Raise RateLimitExceeded if user is sending messages too fast."""
        bucket = self._get_message_bucket(user_id)
        allowed, wait = await bucket.consume()
        if not allowed:
            raise RateLimitExceeded(
                f"You're sending messages too fast! "
                f"Please wait {wait:.0f}s before sending another message.",
                wait_seconds=wait,
            )

    async def check_tool_call(self, user_id: str) -> None:
        """Raise RateLimitExceeded if user has too many tool calls."""
        bucket = self._get_tool_bucket(user_id)
        allowed, wait = await bucket.consume()
        if not allowed:
            raise RateLimitExceeded(
                f"Too many tool calls! "
                f"Please wait {wait:.0f}s before running another tool.",
                wait_seconds=wait,
            )

    async def check_daily_spend(self, cost: float = 0.0) -> None:
        """Raise RateLimitExceeded if daily spend would exceed limit."""
        async with self._daily_spend_lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._spend_reset_date:
                self._daily_spend = 0.0
                self._spend_reset_date = today
            projected = self._daily_spend + cost
            limit = self._max_daily_spend_usd
            if projected > limit:
                raise RateLimitExceeded(
                    f"Daily spending limit reached "
                    f"(${self._daily_spend:.2f}/${limit:.2f}). "
                    f"Processing paused until tomorrow.",
                )

    async def record_spend(self, cost: float) -> float:
        """Record spending. Returns new daily total."""
        async with self._daily_spend_lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._spend_reset_date:
                self._daily_spend = 0.0
                self._spend_reset_date = today
            self._daily_spend += cost
            return self._daily_spend

    async def get_daily_spend(self) -> float:
        async with self._daily_spend_lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._spend_reset_date:
                return 0.0
            return self._daily_spend

    async def update_daily_limit(self, new_limit: float) -> None:
        if new_limit < 0 or new_limit > 100:
            raise ValueError("Budget must be between $0 and $100")
        self._max_daily_spend_usd = new_limit

    def llm_semaphore(self) -> asyncio.Semaphore:
        return self._llm_semaphore

    async def get_status(self, user_id: str) -> dict[str, Any]:
        msg_bucket = self._get_message_bucket(user_id)
        tool_bucket = self._get_tool_bucket(user_id)
        daily_spend = await self.get_daily_spend()
        return {
            "messages_remaining": int(msg_bucket.tokens),
            "messages_limit": self._messages_per_minute,
            "tool_calls_remaining": int(tool_bucket.tokens),
            "tool_calls_limit": self._tool_calls_per_minute,
            "concurrent_llm_available": self._llm_semaphore._value,
            "daily_spend_usd": round(daily_spend, 4),
            "daily_limit_usd": self._max_daily_spend_usd,
            "daily_spend_pct": round(
                (daily_spend / self._max_daily_spend_usd) * 100, 1
            )
            if self._max_daily_spend_usd > 0
            else 0,
        }

    def cleanup_user(self, user_id: str) -> None:
        self._user_message_buckets.pop(user_id, None)
        self._user_tool_buckets.pop(user_id, None)


_rate_limiter: RateLimiter | None = None


def get_rate_limiter(**kwargs: Any) -> RateLimiter:
    """Singleton accessor for the rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(**kwargs)
    return _rate_limiter
