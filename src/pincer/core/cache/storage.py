"""hishel storages for the HTTP caches, under the configured key prefix, in sync and async form.

`redis://` uses hishel's own `RedisStorage` / `AsyncRedisStorage` with
`key_prefix` set under `KeySpace`. `memory://` uses `MemoryStorage` /
`AsyncMemoryStorage` below: hishel 2.x ships no in-memory storage, and its
SQLite one would need `anysqlite` and a file. Entries expire after
`cache_default_ttl` unless the response asks for less (RFC 9111 freshness is
decided by hishel; the TTL only bounds storage).

Neither memory storage defines `__len__`: hishel does `storage or
SqliteStorage()`, so an empty storage that tested falsy would be swapped for a
SQLite file in the working directory. They expose `size` instead.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from hishel import (
    AsyncBaseStorage,
    AsyncRedisStorage,
    BaseStorage,
    Entry,
    Headers,
    RedisStorage,
    Request,
    Response,
)

from pincer.core.cache.backend import for_backend, get_async_redis, get_redis
from pincer.core.cache.keys import KeySpace

DEFAULT_MAX_ENTRIES = 1024
DEFAULT_MAX_STREAM_SIZE = 1024 * 1024  # bytes per response; bigger ones are served uncached


def _iterate(body: bytes) -> Iterator[bytes]:
    yield body


async def _aiterate(body: bytes) -> AsyncIterator[bytes]:
    yield body


@dataclass
class _Stored:
    request: Request
    status_code: int
    headers: Headers
    body: bytes
    index_key: str
    ttl_deadline: float | None
    tti: float | None
    expires_at: float | None


@dataclass(frozen=True)
class _Pending:
    """Everything needed to publish an entry once its body has been read."""

    entry_id: uuid.UUID
    request: Request
    response: Response
    index_key: str
    ttl_deadline: float | None
    tti: float | None


class _Entries:
    """The entry table both memory storages share: a bounded LRU with TTL/TTI expiry, no I/O.

    Lookups are keyed by `<prefix>:idx:<cache key>`, the names hishel's Redis
    storages use, so both backends lay out entries alike.
    """

    def __init__(self, storage: Any, prefix: str, max_entries: int, clock: Callable[[], float]) -> None:
        self._storage = storage  # for hishel's _limits / _expires_at and the configured limits
        self.prefix = prefix
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[uuid.UUID, _Stored] = OrderedDict()
        self._index: dict[str, list[uuid.UUID]] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    def index_key(self, key: str) -> str:
        return f"{self.prefix}:idx:{key}"

    def pending(self, request: Request, response: Response, key: str, ttl: float | None, tti: float | None) -> _Pending:
        ttl_deadline, tti = self._storage._limits(ttl, tti, self._clock())
        return _Pending(uuid.uuid4(), request, response, self.index_key(key), ttl_deadline, tti)

    def fits(self, size: int) -> bool:
        limit = self._storage.max_stream_size
        return limit is None or size <= limit

    def publish(self, pending: _Pending, body: bytes) -> None:
        now = self._clock()
        self._purge_expired(now)
        self._entries[pending.entry_id] = _Stored(
            request=pending.request,
            status_code=pending.response.status_code,
            headers=pending.response.headers,
            body=body,
            index_key=pending.index_key,
            ttl_deadline=pending.ttl_deadline,
            tti=pending.tti,
            expires_at=self._storage._expires_at(pending.ttl_deadline, pending.tti, now),
        )
        self._index.setdefault(pending.index_key, []).append(pending.entry_id)
        while len(self._entries) > self.max_entries:
            self.drop(next(iter(self._entries)))

    def lookup(self, key: str) -> list[tuple[uuid.UUID, _Stored]]:
        """Live entries for `key`; each one returned restarts its TTI and becomes most recently used."""
        now = self._clock()
        self._purge_expired(now)
        found = []
        for entry_id in list(self._index.get(self.index_key(key), ())):
            stored = self._entries[entry_id]
            stored.expires_at = self._storage._expires_at(stored.ttl_deadline, stored.tti, now)
            self._entries.move_to_end(entry_id)
            found.append((entry_id, stored))
        return found

    def update_headers(self, entry_id: uuid.UUID, headers: Headers) -> None:
        stored = self._entries.get(entry_id)
        if stored is not None:
            stored.headers = headers

    def drop(self, entry_id: uuid.UUID) -> None:
        stored = self._entries.pop(entry_id, None)
        if stored is None:
            return
        ids = self._index.get(stored.index_key)
        if ids is not None:
            ids.remove(entry_id)
            if not ids:
                del self._index[stored.index_key]

    def _purge_expired(self, now: float) -> None:
        expired = [i for i, s in self._entries.items() if s.expires_at is not None and s.expires_at <= now]
        for entry_id in expired:
            self.drop(entry_id)


class MemoryStorage(BaseStorage):
    """Sync in-process hishel storage (`hishel.httpx.CacheClient`, `hishel.requests`).

    A lock guards the table, so one storage can serve several threads. Nothing
    is shared between processes.
    """

    def __init__(
        self,
        prefix: str,
        *,
        default_ttl: float | None = None,
        default_tti: float | None = None,
        max_stream_size: int | None = DEFAULT_MAX_STREAM_SIZE,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(default_ttl=default_ttl, default_tti=default_tti, max_stream_size=max_stream_size)
        self._entries = _Entries(self, prefix, max_entries, clock)
        self._lock = threading.Lock()

    @property
    def prefix(self) -> str:
        return self._entries.prefix

    @property
    def size(self) -> int:
        return self._entries.size

    def index_key(self, key: str) -> str:
        return self._entries.index_key(key)

    def create_entry(
        self,
        request: Request,
        response: Response,
        key: str,
        *,
        ttl: float | None = None,
        tti: float | None = None,
    ) -> Entry:
        body = response.stream if response.stream is not None else _iterate(b"")
        assert isinstance(body, Iterator), "Response stream must be an Iterator"
        pending = self._entries.pending(request, response, key, ttl, tti)
        return Entry(id=pending.entry_id, request=request, response=replace(response, stream=self._save(body, pending)))

    def _save(self, body: Iterator[bytes], pending: _Pending) -> Iterator[bytes]:
        """Yield the body unchanged; store the entry once it was read completely and fits."""
        chunks: list[bytes] = []
        size = 0
        saving = True
        for chunk in body:
            if saving:
                size += len(chunk)
                saving = self._entries.fits(size)
                if saving:
                    chunks.append(chunk)
                else:
                    chunks.clear()
            yield chunk
        if saving:
            with self._lock:
                self._entries.publish(pending, b"".join(chunks))

    def get_entries(self, key: str) -> list[Entry]:
        with self._lock:
            found = self._entries.lookup(key)
        return [
            Entry(entry_id, stored.request, Response(stored.status_code, stored.headers, _iterate(stored.body)))
            for entry_id, stored in found
        ]

    def update_headers(self, id: uuid.UUID, headers: Headers) -> None:  # noqa: A002 - hishel's signature
        with self._lock:
            self._entries.update_headers(id, headers)

    def remove_entry(self, id: uuid.UUID) -> None:  # noqa: A002 - hishel's signature
        with self._lock:
            self._entries.drop(id)


class AsyncMemoryStorage(AsyncBaseStorage):
    """Async in-process hishel storage (`ASGICacheMiddleware`, `hishel.httpx.AsyncCacheClient`).

    The table is only touched between awaits, so one event loop needs no lock.
    Nothing is shared between processes.
    """

    def __init__(
        self,
        prefix: str,
        *,
        default_ttl: float | None = None,
        default_tti: float | None = None,
        max_stream_size: int | None = DEFAULT_MAX_STREAM_SIZE,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(default_ttl=default_ttl, default_tti=default_tti, max_stream_size=max_stream_size)
        self._entries = _Entries(self, prefix, max_entries, clock)

    @property
    def prefix(self) -> str:
        return self._entries.prefix

    @property
    def size(self) -> int:
        return self._entries.size

    def index_key(self, key: str) -> str:
        return self._entries.index_key(key)

    async def create_entry(
        self,
        request: Request,
        response: Response,
        key: str,
        *,
        ttl: float | None = None,
        tti: float | None = None,
    ) -> Entry:
        body = response.stream if response.stream is not None else _aiterate(b"")
        assert isinstance(body, AsyncIterator), "Response stream must be an AsyncIterator"
        pending = self._entries.pending(request, response, key, ttl, tti)
        return Entry(id=pending.entry_id, request=request, response=replace(response, stream=self._save(body, pending)))

    async def _save(self, body: AsyncIterator[bytes], pending: _Pending) -> AsyncIterator[bytes]:
        """Yield the body unchanged; store the entry once it was read completely and fits."""
        chunks: list[bytes] = []
        size = 0
        saving = True
        async for chunk in body:
            if saving:
                size += len(chunk)
                saving = self._entries.fits(size)
                if saving:
                    chunks.append(chunk)
                else:
                    chunks.clear()
            yield chunk
        if saving:
            self._entries.publish(pending, b"".join(chunks))

    async def get_entries(self, key: str) -> list[Entry]:
        return [
            Entry(entry_id, stored.request, Response(stored.status_code, stored.headers, _aiterate(stored.body)))
            for entry_id, stored in self._entries.lookup(key)
        ]

    async def update_headers(self, id: uuid.UUID, headers: Headers) -> None:  # noqa: A002 - hishel's signature
        self._entries.update_headers(id, headers)

    async def remove_entry(self, id: uuid.UUID) -> None:  # noqa: A002 - hishel's signature
        self._entries.drop(id)


def create_http_cache_storage(settings: Any, prefix: str | None = None) -> BaseStorage:
    """Sync storage for `settings.cache_backend_url`, under *prefix* (default `KeySpace.http_cache()`)."""
    prefix = prefix or KeySpace.for_cache(settings).http_cache()
    ttl = float(settings.cache_default_ttl)
    url = settings.cache_backend_url
    return for_backend(
        url,
        memory=lambda: MemoryStorage(prefix, default_ttl=ttl),
        redis=lambda: RedisStorage(
            get_redis(url), default_ttl=ttl, max_stream_size=DEFAULT_MAX_STREAM_SIZE, key_prefix=prefix
        ),
    )


def create_async_http_cache_storage(settings: Any, prefix: str | None = None) -> AsyncBaseStorage:
    """Async storage for `settings.cache_backend_url`, under *prefix* (default `KeySpace.http_cache()`)."""
    prefix = prefix or KeySpace.for_cache(settings).http_cache()
    ttl = float(settings.cache_default_ttl)
    url = settings.cache_backend_url
    return for_backend(
        url,
        memory=lambda: AsyncMemoryStorage(prefix, default_ttl=ttl),
        redis=lambda: AsyncRedisStorage(
            get_async_redis(url), default_ttl=ttl, max_stream_size=DEFAULT_MAX_STREAM_SIZE, key_prefix=prefix
        ),
    )
