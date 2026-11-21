"""A small key-value store with TTLs, sync and async, in memory or Redis.

Used by `@cache_response` for endpoint results (JSON bytes). Keys are full
names built by `KeySpace`; `delete_prefix` removes everything under one, e.g.
all cached results of a route.

The memory stores of one `create_*` pair share a table: `AsyncMemoryKeyValueStore`
wraps a `MemoryKeyValueStore`, so a sync and an async endpoint (and an
invalidation from either) see the same entries.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis

DEFAULT_MAX_ENTRIES = 4096
_SCAN_BATCH = 500


class KeyValueStore(ABC):
    """Sync store of bytes under full key names, each with an optional TTL."""

    @abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    def set(self, key: str, value: bytes, ttl: float | None = None) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with `prefix`; the number deleted."""


class AsyncKeyValueStore(ABC):
    """The async twin of `KeyValueStore`."""

    @abstractmethod
    async def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with `prefix`; the number deleted."""


class MemoryKeyValueStore(KeyValueStore):
    """In-process, thread-safe: a bounded LRU with per-key expiry. Not shared between processes."""

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES, clock: Callable[[], float] = time.time) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._items)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            value, expires_at = item
            if expires_at is not None and expires_at <= self._clock():
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        with self._lock:
            self._items[key] = (value, None if ttl is None else self._clock() + ttl)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            doomed = [key for key in self._items if key.startswith(prefix)]
            for key in doomed:
                del self._items[key]
            return len(doomed)


class AsyncMemoryKeyValueStore(AsyncKeyValueStore):
    """Async face of a `MemoryKeyValueStore` (no I/O, so nothing to await)."""

    def __init__(self, store: MemoryKeyValueStore | None = None) -> None:
        self.store = store or MemoryKeyValueStore()

    @property
    def size(self) -> int:
        return self.store.size

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        self.store.set(key, value, ttl)

    async def delete(self, key: str) -> None:
        self.store.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        return self.store.delete_prefix(prefix)


def _px(ttl: float | None) -> int | None:
    return None if ttl is None else max(1, int(ttl * 1000))


def _match(prefix: str) -> str:
    """A SCAN pattern for keys starting with `prefix` (glob characters escaped)."""
    escaped = "".join(f"\\{ch}" if ch in "*?[]\\" else ch for ch in prefix)
    return escaped + "*"


class RedisKeyValueStore(KeyValueStore):
    """Sync Redis (`redis.Redis`): `SET … PX`, prefix deletes by `SCAN` + `UNLINK`."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    def get(self, key: str) -> bytes | None:
        value: Any = self._client.get(key)
        return None if value is None else bytes(value)

    def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        self._client.set(key, value, px=_px(ttl))

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        batch: list[Any] = []
        for key in self._client.scan_iter(match=_match(prefix), count=_SCAN_BATCH):
            batch.append(key)
            if len(batch) >= _SCAN_BATCH:
                deleted += self._unlink(batch)
                batch.clear()
        if batch:
            deleted += self._unlink(batch)
        return deleted

    def _unlink(self, keys: list[Any]) -> int:
        count: Any = self._client.unlink(*keys)
        return int(count)


class AsyncRedisKeyValueStore(AsyncKeyValueStore):
    """Async Redis (`redis.asyncio.Redis`), the twin of `RedisKeyValueStore`."""

    def __init__(self, client: AsyncRedis) -> None:
        self._client = client

    async def get(self, key: str) -> bytes | None:
        value: Any = await self._client.get(key)
        return None if value is None else bytes(value)

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        await self._client.set(key, value, px=_px(ttl))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        batch: list[Any] = []
        async for key in self._client.scan_iter(match=_match(prefix), count=_SCAN_BATCH):
            batch.append(key)
            if len(batch) >= _SCAN_BATCH:
                deleted += int(await self._client.unlink(*batch))
                batch.clear()
        if batch:
            deleted += int(await self._client.unlink(*batch))
        return deleted


def create_kv_stores(backend_url: Any) -> tuple[KeyValueStore, AsyncKeyValueStore]:
    """A sync and an async store on the same data: one Redis, or one shared in-memory table."""
    from pincer.core.cache import backend

    def in_memory() -> tuple[KeyValueStore, AsyncKeyValueStore]:
        memory = MemoryKeyValueStore()
        return memory, AsyncMemoryKeyValueStore(memory)

    def in_redis() -> tuple[KeyValueStore, AsyncKeyValueStore]:
        return (
            RedisKeyValueStore(backend.get_redis(backend_url)),
            AsyncRedisKeyValueStore(backend.get_async_redis(backend_url)),
        )

    return backend.for_backend(backend_url, memory=in_memory, redis=in_redis)
