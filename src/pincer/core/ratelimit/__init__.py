"""Async request rate limiting: strategies, memory/Redis storage, a limiter and ASGI middleware.

The algorithm design follows awais7012/FastAPI-RateLimiter
(https://github.com/awais7012/FastAPI-RateLimiter); this is a clean
reimplementation, not a copy. It differs where pincer needs it to:

- async end to end (`redis.asyncio`), so a limit check never blocks the event loop;
- every key under the configured prefix (`pincer.core.cache.keys.KeySpace`);
- one atomic Lua script per check on Redis, and TTLs on every key in both backends;
- pure-ASGI middleware with `429` / `Retry-After` / `X-RateLimit-*` responses;
- fail-open: a broken backend logs a warning and lets requests through.
"""

from __future__ import annotations

from pincer.core.ratelimit.limiter import Limiter
from pincer.core.ratelimit.middleware import RateLimitMiddleware
from pincer.core.ratelimit.storage import MemoryStorage, RateLimitStorage, RedisStorage, create_storage
from pincer.core.ratelimit.strategies import SlidingWindowStrategy, Strategy, TokenBucketStrategy
from pincer.core.ratelimit.types import Rate, RateLimitResult

__all__ = [
    "Limiter",
    "MemoryStorage",
    "Rate",
    "RateLimitMiddleware",
    "RateLimitResult",
    "RateLimitStorage",
    "RedisStorage",
    "SlidingWindowStrategy",
    "Strategy",
    "TokenBucketStrategy",
    "create_storage",
]
