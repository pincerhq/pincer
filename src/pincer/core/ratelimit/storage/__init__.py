"""Where rate-limit buckets live: in-process memory or a shared Redis."""

from __future__ import annotations

from typing import Any

from pincer.core.ratelimit.storage.base import RateLimitStorage
from pincer.core.ratelimit.storage.memory import MemoryStorage
from pincer.core.ratelimit.storage.redis import RedisStorage

__all__ = ["MemoryStorage", "RateLimitStorage", "RedisStorage", "create_storage"]


def create_storage(backend_url: Any) -> RateLimitStorage:
    """`RedisStorage` for a `redis://` URL, else `MemoryStorage` (`memory://`)."""
    from pincer.config.dsn import is_redis

    if is_redis(backend_url):
        from pincer.core.cache.backend import get_redis

        return RedisStorage(get_redis(backend_url))
    return MemoryStorage()
