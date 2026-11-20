"""Async request rate limiting: strategies, memory/Redis storage, a limiter and ASGI middleware.

The algorithm design follows awais7012/FastAPI-RateLimiter
(https://github.com/awais7012/FastAPI-RateLimiter); this is a clean
reimplementation, not a copy. It differs where pincer needs it to:

- sync and async twins (`Limiter` / `AsyncLimiter`, `MemoryStorage` /
  `AsyncMemoryStorage`, `RedisStorage` / `AsyncRedisStorage`) sharing logic and
  buckets; the async ones (`redis.asyncio`) never block the event loop;
- every key under the configured prefix (`pincer.core.cache.keys.KeySpace`);
- one atomic Lua script per check on Redis, and TTLs on every key in both backends;
- pure-ASGI middleware with `429` / `Retry-After` / `X-RateLimit-*` responses;
- fail-open: a broken backend logs a warning and lets requests through.
"""

from __future__ import annotations

from pincer.core.ratelimit.decorators import LimitScope, RouteLimit, no_rate_limit, rate_limit
from pincer.core.ratelimit.limiter import (
    AsyncLimiter,
    AsyncLimiterFactory,
    Charge,
    Limiter,
    LimiterFactory,
    acharge_all,
    charge_all,
)
from pincer.core.ratelimit.middleware import RateLimitMiddleware
from pincer.core.ratelimit.storage import (
    AsyncMemoryStorage,
    AsyncRateLimitStorage,
    AsyncRedisStorage,
    MemoryStorage,
    RateLimitStorage,
    RedisStorage,
    create_async_storage,
    create_storage,
)
from pincer.core.ratelimit.strategies import SlidingWindowStrategy, Strategy, TokenBucketStrategy
from pincer.core.ratelimit.types import Rate, RateLimitResult

__all__ = [
    "AsyncLimiter",
    "AsyncLimiterFactory",
    "Charge",
    "AsyncMemoryStorage",
    "AsyncRateLimitStorage",
    "AsyncRedisStorage",
    "LimitScope",
    "Limiter",
    "LimiterFactory",
    "MemoryStorage",
    "Rate",
    "RateLimitMiddleware",
    "RateLimitResult",
    "RateLimitStorage",
    "RedisStorage",
    "RouteLimit",
    "SlidingWindowStrategy",
    "Strategy",
    "TokenBucketStrategy",
    "acharge_all",
    "charge_all",
    "create_async_storage",
    "create_storage",
    "no_rate_limit",
    "rate_limit",
]
