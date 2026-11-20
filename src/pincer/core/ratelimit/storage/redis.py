"""Buckets in Redis: one atomic Lua script per check, shared by every app process.

`RedisStorage` takes a sync `redis.Redis`, `AsyncRedisStorage` a
`redis.asyncio.Redis`. Both run the strategy's own `script` through redis-py's
`register_script` (`EVALSHA`, reloaded on `NOSCRIPT`), so sync and async
callers share buckets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.core.ratelimit.storage.base import AsyncRateLimitStorage, RateLimitStorage
from pincer.core.ratelimit.types import Rate, RateLimitResult

if TYPE_CHECKING:
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis

    from pincer.core.ratelimit.strategies import Strategy


def _parse(reply: Any, rate: Rate) -> RateLimitResult:
    """A script's `{allowed, remaining, reset_after_ms, retry_after_ms}` reply."""
    allowed, remaining, reset_ms, retry_ms = reply
    return RateLimitResult(
        allowed=bool(int(allowed)),
        limit=rate.limit,
        remaining=int(remaining),
        reset_after=int(reset_ms) / 1000,
        retry_after=int(retry_ms) / 1000,
    )


def _args(rate: Rate, cost: int, dry_run: bool) -> list[Any]:
    return [rate.limit, rate.period, cost, 1 if dry_run else 0]


class _Scripts:
    """One registered script per strategy, per client."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._scripts: dict[str, Any] = {}

    def __call__(self, strategy: Strategy) -> Any:
        script = self._scripts.get(strategy.name)
        if script is None:
            script = self._client.register_script(strategy.script)
            self._scripts[strategy.name] = script
        return script


class RedisStorage(RateLimitStorage):
    """Sync Redis buckets (`redis.Redis`)."""

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._script = _Scripts(client)

    def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1, *, dry_run: bool = False) -> RateLimitResult:
        return _parse(self._script(strategy)(keys=[key], args=_args(rate, cost, dry_run)), rate)

    def reset(self, key: str) -> None:
        self._client.delete(key)


class AsyncRedisStorage(AsyncRateLimitStorage):
    """Async Redis buckets (`redis.asyncio.Redis`)."""

    def __init__(self, client: AsyncRedis) -> None:
        self._client = client
        self._script = _Scripts(client)

    async def hit(
        self, key: str, strategy: Strategy, rate: Rate, cost: int = 1, *, dry_run: bool = False
    ) -> RateLimitResult:
        return _parse(await self._script(strategy)(keys=[key], args=_args(rate, cost, dry_run)), rate)

    async def reset(self, key: str) -> None:
        await self._client.delete(key)
