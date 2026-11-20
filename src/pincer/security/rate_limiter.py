"""
Pincer Rate Limiter — per-user message and tool-call limits for the channels, plus the LLM semaphore.

- Per-user: messages/min, tool calls/min — token buckets on `pincer.core.ratelimit`,
  stored in the cache backend (`PINCER_CACHE_BACKEND_URL`) under
  `<prefix>:kv:ratelimit:channel_message|channel_tool:<user>`. With Redis the
  budget holds across `pincer run` / `pincer run tasks` processes and restarts.
- Global: concurrent LLM calls (an in-process semaphore).
- Spend and the daily budget live in the cost service (pincer.services.costs).
- Fails open: a broken backend never stops the agent from answering.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import (
    AsyncLimiter,
    AsyncMemoryStorage,
    AsyncRateLimitStorage,
    Rate,
    TokenBucketStrategy,
)
from pincer.exceptions import RateLimitExceeded

_DEFAULT_KEYS = KeySpace("local:pincer")


class RateLimiter:
    """Per-user + global rate limiter for the Pincer agent.

    Args:
        messages_per_minute: Messages one user may send per minute (bursts up to it).
        tool_calls_per_minute: Tool calls one user may trigger per minute.
        max_concurrent_llm: LLM requests in flight at once, in this process.
        storage: Where the buckets live; in-process memory when omitted.
        keys: Names the buckets; `KeySpace.for_cache(settings)` in the app.
    """

    def __init__(
        self,
        messages_per_minute: int = 30,
        tool_calls_per_minute: int = 20,
        max_concurrent_llm: int = 5,
        *,
        storage: AsyncRateLimitStorage | None = None,
        keys: KeySpace | None = None,
    ) -> None:
        storage = storage or AsyncMemoryStorage()
        keys = keys or _DEFAULT_KEYS
        self._messages = AsyncLimiter(
            storage, Rate.per_minute(messages_per_minute), keys, "channel_message", TokenBucketStrategy()
        )
        self._tools = AsyncLimiter(
            storage, Rate.per_minute(tool_calls_per_minute), keys, "channel_tool", TokenBucketStrategy()
        )
        self._llm_semaphore = asyncio.Semaphore(max_concurrent_llm)

    async def check_message(self, user_id: str) -> None:
        """Raise RateLimitExceeded if user is sending messages too fast."""
        result = await self._messages.hit(user_id)
        if not result.allowed:
            wait = result.retry_after
            raise RateLimitExceeded(
                f"You're sending messages too fast! Please wait {math.ceil(wait)}s before sending another message.",
                wait_seconds=wait,
            )

    async def check_tool_call(self, user_id: str) -> None:
        """Raise RateLimitExceeded if user has too many tool calls."""
        result = await self._tools.hit(user_id)
        if not result.allowed:
            wait = result.retry_after
            raise RateLimitExceeded(
                f"Too many tool calls! Please wait {math.ceil(wait)}s before running another tool.",
                wait_seconds=wait,
            )

    def llm_semaphore(self) -> asyncio.Semaphore:
        return self._llm_semaphore

    async def get_status(self, user_id: str) -> dict[str, Any]:
        """What the user has left, read without spending anything."""

        async def remaining(limiter: AsyncLimiter) -> int:
            result = await limiter.check(user_id)
            # `check` reports what would be left after one more; add it back.
            return result.remaining + 1 if result.allowed else 0

        return {
            "messages_remaining": await remaining(self._messages),
            "messages_limit": self._messages.rate.limit,
            "tool_calls_remaining": await remaining(self._tools),
            "tool_calls_limit": self._tools.rate.limit,
            "concurrent_llm_available": self._llm_semaphore._value,
        }

    async def cleanup_user(self, user_id: str) -> None:
        """Forget the user's buckets (a fresh budget)."""
        await self._messages.reset(user_id)
        await self._tools.reset(user_id)


_rate_limiter: RateLimiter | None = None


def get_rate_limiter(**kwargs: Any) -> RateLimiter:
    """Singleton accessor for the rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(**kwargs)
    return _rate_limiter
