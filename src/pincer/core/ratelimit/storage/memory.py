"""In-process buckets. Each worker process limits on its own: use Redis to share limits."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pincer.core.ratelimit.storage.base import RateLimitStorage

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.core.ratelimit.strategies import Strategy
    from pincer.core.ratelimit.types import Rate, RateLimitResult

# Expired buckets are swept at most this often, and whenever the table grows past max_keys.
SWEEP_INTERVAL = 60.0
DEFAULT_MAX_KEYS = 100_000


class MemoryStorage(RateLimitStorage):
    """A dict of bucket states with per-key expiry, so idle buckets don't pile up.

    `hit` never awaits between reading and writing a bucket, so on one event
    loop it is atomic without a lock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[list[float], float]] = {}
        self._next_sweep = 0.0

    def __len__(self) -> int:
        return len(self._buckets)

    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        now = self._clock()
        self._sweep(now)
        stored = self._buckets.get(key)
        state = stored[0] if stored is not None and stored[1] > now else None
        new_state, result = strategy.apply(state, now, rate, cost)
        self._buckets[key] = (new_state, now + strategy.ttl(rate))
        return result

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    def _sweep(self, now: float) -> None:
        if now < self._next_sweep and len(self._buckets) < self._max_keys:
            return
        self._next_sweep = now + SWEEP_INTERVAL
        for key in [k for k, (_, expires_at) in self._buckets.items() if expires_at <= now]:
            del self._buckets[key]
        # Still full of live buckets (a flood of distinct keys): drop the oldest-inserted ones.
        overflow = len(self._buckets) - self._max_keys
        if overflow > 0:
            for key in list(self._buckets)[:overflow]:
                del self._buckets[key]
