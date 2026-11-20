from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pincer.core.log_once import LogOnce
from pincer.core.ratelimit.strategies import SlidingWindowStrategy, Strategy
from pincer.core.ratelimit.types import Rate, RateLimitResult

if TYPE_CHECKING:
    from pincer.core.cache.keys import KeySpace
    from pincer.core.ratelimit.storage import RateLimitStorage

logger = logging.getLogger(__name__)


class Limiter:
    """One rate limit (`rate` per bucket) for one scope, e.g. `ip` or `user`.

    Buckets are named `KeySpace.ratelimit(scope, identifier)`. A storage
    failure fails open: the request is allowed and a warning is logged once
    per outage (recovery at INFO), so a Redis outage never takes the API down.
    """

    def __init__(
        self,
        storage: RateLimitStorage,
        rate: Rate,
        keys: KeySpace,
        scope: str,
        strategy: Strategy | None = None,
    ) -> None:
        self.storage = storage
        self.rate = rate
        self.keys = keys
        self.scope = scope
        self.strategy: Strategy = strategy or SlidingWindowStrategy()
        self._outage = LogOnce(logger, recovery=f"Rate limit backend for scope {scope!r} recovered")

    def key(self, identifier: str) -> str:
        return self.keys.ratelimit(self.scope, identifier)

    async def hit(self, identifier: str, cost: int = 1) -> RateLimitResult:
        try:
            result = await self.storage.hit(self.key(identifier), self.strategy, self.rate, cost)
        except Exception:  # noqa: BLE001 - fail open on any backend error
            self._outage.log(
                "Rate limit backend failed for scope %r; allowing requests until it recovers",
                self.scope,
                exc_info=True,
            )
            return RateLimitResult.unlimited(self.rate.limit)
        self._outage.clear()
        return result

    async def reset(self, identifier: str) -> None:
        await self.storage.reset(self.key(identifier))
