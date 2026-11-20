"""`Limiter` (sync) and `AsyncLimiter` (async): one rate limit for one scope, failing open."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pincer.core.log_once import LogOnce
from pincer.core.ratelimit.strategies import SlidingWindowStrategy, Strategy
from pincer.core.ratelimit.types import Rate, RateLimitResult

if TYPE_CHECKING:
    from pincer.core.cache.keys import KeySpace
    from pincer.core.ratelimit.storage import AsyncRateLimitStorage, RateLimitStorage

logger = logging.getLogger(__name__)


class _LimiterBase:
    """What both limiters share: the rule, the bucket names and the fail-open bookkeeping.

    Buckets are named `KeySpace.ratelimit(scope, identifier)`, so a sync and an
    async limiter with the same scope on one backend count the same requests.
    A storage failure fails open by default: the request is allowed and a
    warning is logged once per outage (recovery at INFO), so a Redis outage
    never takes the caller down. With `fail_open=False` it is refused instead
    (`RateLimitResult.unavailable`), for limits that guard real money, e.g.
    the website's demo calls. Either way the result has `failed=True`.
    """

    def __init__(
        self,
        rate: Rate,
        keys: KeySpace,
        scope: str,
        strategy: Strategy | None,
        route: str | None = None,
        fail_open: bool = True,
    ) -> None:
        self.fail_open = fail_open
        self.rate = rate
        self.keys = keys
        self.scope = scope
        self.route = route
        self.strategy: Strategy = strategy or SlidingWindowStrategy()
        label = scope if route is None else f"{scope}:{route}"
        self._outage = LogOnce(logger, recovery=f"Rate limit backend for scope {label!r} recovered")

    def key(self, identifier: str) -> str:
        return self.keys.ratelimit(self.scope, identifier, route=self.route)

    def _failed(self) -> RateLimitResult:
        self._outage.log(
            "Rate limit backend failed for scope %r; %s requests until it recovers",
            self.scope,
            "allowing" if self.fail_open else "refusing",
            exc_info=True,
        )
        if self.fail_open:
            return RateLimitResult.unlimited(self.rate.limit)
        return RateLimitResult.unavailable(self.rate.limit)

    def _succeeded(self, result: RateLimitResult) -> RateLimitResult:
        self._outage.clear()
        return result


class Limiter(_LimiterBase):
    """Sync limiter over a `RateLimitStorage` (`MemoryStorage`, `RedisStorage`)."""

    def __init__(
        self,
        storage: RateLimitStorage,
        rate: Rate,
        keys: KeySpace,
        scope: str,
        strategy: Strategy | None = None,
        *,
        route: str | None = None,
        fail_open: bool = True,
    ) -> None:
        super().__init__(rate, keys, scope, strategy, route, fail_open)
        self.storage = storage

    def hit(self, identifier: str, cost: int = 1) -> RateLimitResult:
        """Charge `cost` to `identifier`'s bucket."""
        try:
            result = self.storage.hit(self.key(identifier), self.strategy, self.rate, cost)
        except Exception:  # noqa: BLE001 - fail open on any backend error
            return self._failed()
        return self._succeeded(result)

    def check(self, identifier: str, cost: int = 1) -> RateLimitResult:
        """Whether `cost` would be allowed now, without charging it."""
        try:
            result = self.storage.hit(self.key(identifier), self.strategy, self.rate, cost, dry_run=True)
        except Exception:  # noqa: BLE001 - fail open on any backend error
            return self._failed()
        return self._succeeded(result)

    def reset(self, identifier: str) -> None:
        self.storage.reset(self.key(identifier))


class AsyncLimiter(_LimiterBase):
    """Async limiter over an `AsyncRateLimitStorage` (`AsyncMemoryStorage`, `AsyncRedisStorage`)."""

    def __init__(
        self,
        storage: AsyncRateLimitStorage,
        rate: Rate,
        keys: KeySpace,
        scope: str,
        strategy: Strategy | None = None,
        *,
        route: str | None = None,
        fail_open: bool = True,
    ) -> None:
        super().__init__(rate, keys, scope, strategy, route, fail_open)
        self.storage = storage

    async def hit(self, identifier: str, cost: int = 1) -> RateLimitResult:
        """Charge `cost` to `identifier`'s bucket."""
        try:
            result = await self.storage.hit(self.key(identifier), self.strategy, self.rate, cost)
        except Exception:  # noqa: BLE001 - fail open on any backend error
            return self._failed()
        return self._succeeded(result)

    async def check(self, identifier: str, cost: int = 1) -> RateLimitResult:
        """Whether `cost` would be allowed now, without charging it."""
        try:
            result = await self.storage.hit(self.key(identifier), self.strategy, self.rate, cost, dry_run=True)
        except Exception:  # noqa: BLE001 - fail open on any backend error
            return self._failed()
        return self._succeeded(result)

    async def reset(self, identifier: str) -> None:
        await self.storage.reset(self.key(identifier))
