"""Buckets in Redis: one atomic Lua script per check, shared by every app process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.core.ratelimit.types import Rate, RateLimitResult

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from pincer.core.ratelimit.strategies import Strategy


class RedisStorage:
    """Runs each strategy's Lua script via `EVALSHA` (redis-py `Script` reloads it on `NOSCRIPT`)."""

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._scripts: dict[str, Any] = {}

    def _script(self, strategy: Strategy) -> Any:
        script = self._scripts.get(strategy.name)
        if script is None:
            script = self._client.register_script(strategy.script)
            self._scripts[strategy.name] = script
        return script

    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        allowed, remaining, reset_ms, retry_ms = await self._script(strategy)(
            keys=[key], args=[rate.limit, rate.period, cost]
        )
        return RateLimitResult(
            allowed=bool(int(allowed)),
            limit=rate.limit,
            remaining=int(remaining),
            reset_after=int(reset_ms) / 1000,
            retry_after=int(retry_ms) / 1000,
        )

    async def reset(self, key: str) -> None:
        await self._client.delete(key)
