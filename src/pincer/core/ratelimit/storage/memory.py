"""In-process buckets. Each worker process limits on its own: use Redis to share limits."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from pincer.core.ratelimit.storage.base import AsyncRateLimitStorage, RateLimitStorage

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.core.ratelimit.strategies import Strategy
    from pincer.core.ratelimit.types import Rate, RateLimitResult

# Expired buckets are swept at most this often, and whenever the table grows past max_keys.
SWEEP_INTERVAL = 60.0
DEFAULT_MAX_KEYS = 100_000


class _Buckets:
    """The bucket table both memory storages share: per-key expiry, a size bound, no I/O."""

    def __init__(self, clock: Callable[[], float], max_keys: int) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[list[float], float]] = {}
        self._next_sweep = 0.0

    @property
    def size(self) -> int:
        return len(self._buckets)

    def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int) -> RateLimitResult:
        now = self._clock()
        self._sweep(now)
        stored = self._buckets.get(key)
        state = stored[0] if stored is not None and stored[1] > now else None
        new_state, result = strategy.apply(state, now, rate, cost)
        self._buckets[key] = (new_state, now + strategy.ttl(rate))
        return result

    def reset(self, key: str) -> None:
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


class MemoryStorage(RateLimitStorage):
    """Sync in-process buckets. A lock makes each check atomic across threads."""

    def __init__(self, *, clock: Callable[[], float] = time.time, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._buckets = _Buckets(clock, max_keys)
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return self._buckets.size

    def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        with self._lock:
            return self._buckets.hit(key, strategy, rate, cost)

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.reset(key)


class AsyncMemoryStorage(AsyncRateLimitStorage):
    """Async in-process buckets.

    A check never awaits between reading and writing a bucket, so on one event
    loop it is atomic without a lock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._buckets = _Buckets(clock, max_keys)

    @property
    def size(self) -> int:
        return self._buckets.size

    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        return self._buckets.hit(key, strategy, rate, cost)

    async def reset(self, key: str) -> None:
        self._buckets.reset(key)
