"""Where rate-limit buckets live: in-process memory or a shared Redis, each in a sync and an async form."""

from __future__ import annotations

from typing import Any

from pincer.core.ratelimit.storage.base import AsyncRateLimitStorage, RateLimitStorage
from pincer.core.ratelimit.storage.memory import AsyncMemoryStorage, MemoryStorage
from pincer.core.ratelimit.storage.redis import AsyncRedisStorage, RedisStorage

__all__ = [
    "AsyncMemoryStorage",
    "AsyncRateLimitStorage",
    "AsyncRedisStorage",
    "MemoryStorage",
    "RateLimitStorage",
    "RedisStorage",
    "create_async_storage",
    "create_storage",
]


def create_storage(backend_url: Any) -> RateLimitStorage:
    """Sync storage: `RedisStorage` for a `redis://` URL, else `MemoryStorage` (`memory://`)."""
    from pincer.core.cache import backend

    return backend.for_backend(
        backend_url, memory=MemoryStorage, redis=lambda: RedisStorage(backend.get_redis(backend_url))
    )


def create_async_storage(backend_url: Any) -> AsyncRateLimitStorage:
    """Async storage: `AsyncRedisStorage` for a `redis://` URL, else `AsyncMemoryStorage` (`memory://`)."""
    from pincer.core.cache import backend

    return backend.for_backend(
        backend_url, memory=AsyncMemoryStorage, redis=lambda: AsyncRedisStorage(backend.get_async_redis(backend_url))
    )
