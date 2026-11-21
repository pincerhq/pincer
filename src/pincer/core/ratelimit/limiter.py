"""`Limiter` (sync) and `AsyncLimiter` (async): one rate limit for one scope, failing open.

Also what builds and combines them:

- `LimiterFactory` / `AsyncLimiterFactory`: limiters on one storage and key
  space, e.g. `AsyncLimiterFactory.from_settings(settings)` for the cache backend.
- `Charge` + `charge_all` / `acharge_all`: several limits that must all allow a
  request. They are checked first and charged only together, so a request one
  limit refuses spends nothing on the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pincer.core.log_once import LogOnce
from pincer.core.ratelimit.strategies import SlidingWindowStrategy, Strategy
from pincer.core.ratelimit.types import DEFAULT_DETAIL, Detail, Rate, RateLimitResult, detail_text

if TYPE_CHECKING:
    from collections.abc import Sequence

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


@dataclass(frozen=True)
class LimiterFactory:
    """Sync limiters on one storage and key space."""

    storage: RateLimitStorage
    keys: KeySpace

    @classmethod
    def from_settings(cls, settings: Any) -> LimiterFactory:
        """On the cache backend (`cache_backend_url`) under the cache prefix."""
        from pincer.core.cache.keys import KeySpace
        from pincer.core.ratelimit.storage import create_storage

        return cls(create_storage(settings.cache_backend_url), KeySpace.for_cache(settings))

    def limiter(
        self,
        rate: Rate,
        scope: str,
        strategy: Strategy | None = None,
        *,
        route: str | None = None,
        fail_open: bool = True,
    ) -> Limiter:
        return Limiter(self.storage, rate, self.keys, scope, strategy, route=route, fail_open=fail_open)


@dataclass(frozen=True)
class AsyncLimiterFactory:
    """Async limiters on one storage and key space."""

    storage: AsyncRateLimitStorage
    keys: KeySpace

    @classmethod
    def from_settings(cls, settings: Any) -> AsyncLimiterFactory:
        """On the cache backend (`cache_backend_url`) under the cache prefix."""
        from pincer.core.cache.keys import KeySpace
        from pincer.core.ratelimit.storage import create_async_storage

        return cls(create_async_storage(settings.cache_backend_url), KeySpace.for_cache(settings))

    def limiter(
        self,
        rate: Rate,
        scope: str,
        strategy: Strategy | None = None,
        *,
        route: str | None = None,
        fail_open: bool = True,
    ) -> AsyncLimiter:
        return AsyncLimiter(self.storage, rate, self.keys, scope, strategy, route=route, fail_open=fail_open)


@dataclass(frozen=True)
class Charge[L: (Limiter, AsyncLimiter)]:
    """One limit a request must pass: the limiter, whose bucket, how much, and what to say on refusal."""

    limiter: L
    identifier: str
    cost: int = 1
    detail: Detail = DEFAULT_DETAIL

    def message(self, result: RateLimitResult) -> str:
        return detail_text(self.detail, result)


def _reported(results: Sequence[RateLimitResult]) -> int:
    """Which result to report: a refusal with the longest wait, else the one with the least room left."""
    denied = [i for i, r in enumerate(results) if not r.allowed]
    if denied:
        return max(denied, key=lambda i: results[i].retry_after)
    return min(range(len(results)), key=lambda i: (results[i].remaining / results[i].limit, results[i].remaining))


def charge_all(charges: Sequence[Charge[Limiter]]) -> tuple[Charge[Limiter], RateLimitResult]:
    """Check every charge, then spend them all, or none when one refuses.

    Returns the charge to report with its result: the first refusal of the
    check (nothing spent), else a refusal of the charge itself (two requests
    raced past the check), else the one with the least room left.
    """
    if not charges:
        raise ValueError("charge_all needs at least one charge")
    for charge in charges:
        checked = charge.limiter.check(charge.identifier, charge.cost)
        if not checked.allowed:
            return charge, checked
    results = [charge.limiter.hit(charge.identifier, charge.cost) for charge in charges]
    index = _reported(results)
    return charges[index], results[index]


async def acharge_all(charges: Sequence[Charge[AsyncLimiter]]) -> tuple[Charge[AsyncLimiter], RateLimitResult]:
    """The async twin of `charge_all`."""
    if not charges:
        raise ValueError("acharge_all needs at least one charge")
    for charge in charges:
        checked = await charge.limiter.check(charge.identifier, charge.cost)
        if not checked.allowed:
            return charge, checked
    results = [await charge.limiter.hit(charge.identifier, charge.cost) for charge in charges]
    index = _reported(results)
    return charges[index], results[index]
