"""hishel storages for the HTTP response cache, under the configured key prefix.

`redis://` uses hishel's own `AsyncRedisStorage` with `key_prefix` set to
`KeySpace.http_cache()`. `memory://` uses `AsyncMemoryStorage` below: hishel
2.x ships no in-memory storage, and its SQLite one would need `anysqlite` and a
file. Both expire entries after `cache_default_ttl` unless the response asks
for less (RFC 9111 freshness is decided by hishel, the TTL only bounds storage).
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

from hishel import AsyncBaseStorage, AsyncRedisStorage, Entry, Headers, Request, Response

from pincer.config.dsn import is_redis
from pincer.core.cache.backend import get_redis
from pincer.core.cache.keys import KeySpace

DEFAULT_MAX_ENTRIES = 1024
DEFAULT_MAX_STREAM_SIZE = 1024 * 1024  # bytes per response; bigger ones are served uncached


async def _iterate(body: bytes) -> AsyncIterator[bytes]:
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


class AsyncMemoryStorage(AsyncBaseStorage):
    """In-process hishel storage: a bounded LRU of complete entries with TTL/TTI expiry.

    Lookups are keyed by `<prefix>:idx:<cache key>`, the same names
    `AsyncRedisStorage` uses, so both backends lay out entries alike. Nothing is
    shared between processes: each worker has its own cache.
    """

    def __init__(
        self,
        prefix: str,
        *,
        default_ttl: float | None = None,
        default_tti: float | None = None,
        max_stream_size: int | None = DEFAULT_MAX_STREAM_SIZE,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Any = time.time,
    ) -> None:
        super().__init__(default_ttl=default_ttl, default_tti=default_tti, max_stream_size=max_stream_size)
        self.prefix = prefix
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[uuid.UUID, _Stored] = OrderedDict()
        self._index: dict[str, list[uuid.UUID]] = {}

    def index_key(self, key: str) -> str:
        return f"{self.prefix}:idx:{key}"

    @property
    def size(self) -> int:
        """Entries stored. (Not `__len__`: hishel treats a falsy storage as none and opens SQLite.)"""
        return len(self._entries)

    async def create_entry(
        self,
        request: Request,
        response: Response,
        key: str,
        *,
        ttl: float | None = None,
        tti: float | None = None,
    ) -> Entry:
        body = response.stream if response.stream is not None else _iterate(b"")
        assert isinstance(body, AsyncIterator), "Response stream must be an AsyncIterator"
        ttl_deadline, tti = self._limits(ttl, tti, self._clock())
        entry_id = uuid.uuid4()
        stream = self._save_stream(body, entry_id, request, response, self.index_key(key), ttl_deadline, tti)
        return Entry(id=entry_id, request=request, response=replace(response, stream=stream))

    async def _save_stream(
        self,
        body: AsyncIterator[bytes],
        entry_id: uuid.UUID,
        request: Request,
        response: Response,
        index_key: str,
        ttl_deadline: float | None,
        tti: float | None,
    ) -> AsyncIterator[bytes]:
        """Yield the body unchanged; store the entry once it was read completely and fits."""
        chunks: list[bytes] = []
        size = 0
        saving = True
        async for chunk in body:
            if saving:
                size += len(chunk)
                if self.max_stream_size is not None and size > self.max_stream_size:
                    saving = False
                    chunks.clear()
                else:
                    chunks.append(chunk)
            yield chunk
        if saving:
            self._publish(entry_id, request, response, b"".join(chunks), index_key, ttl_deadline, tti)

    def _publish(
        self,
        entry_id: uuid.UUID,
        request: Request,
        response: Response,
        body: bytes,
        index_key: str,
        ttl_deadline: float | None,
        tti: float | None,
    ) -> None:
        now = self._clock()
        self._purge_expired(now)
        self._entries[entry_id] = _Stored(
            request=request,
            status_code=response.status_code,
            headers=response.headers,
            body=body,
            index_key=index_key,
            ttl_deadline=ttl_deadline,
            tti=tti,
            expires_at=self._expires_at(ttl_deadline, tti, now),
        )
        self._index.setdefault(index_key, []).append(entry_id)
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            self._drop(oldest)

    async def get_entries(self, key: str) -> list[Entry]:
        now = self._clock()
        self._purge_expired(now)
        entries = []
        for entry_id in list(self._index.get(self.index_key(key), ())):
            stored = self._entries[entry_id]
            stored.expires_at = self._expires_at(stored.ttl_deadline, stored.tti, now)  # restart the TTI
            self._entries.move_to_end(entry_id)  # most recently used
            entries.append(
                Entry(
                    id=entry_id,
                    request=stored.request,
                    response=Response(
                        status_code=stored.status_code, headers=stored.headers, stream=_iterate(stored.body)
                    ),
                )
            )
        return entries

    async def update_headers(self, id: uuid.UUID, headers: Headers) -> None:  # noqa: A002 - hishel's signature
        stored = self._entries.get(id)
        if stored is not None:
            stored.headers = headers

    async def remove_entry(self, id: uuid.UUID) -> None:  # noqa: A002 - hishel's signature
        self._drop(id)

    def _drop(self, entry_id: uuid.UUID) -> None:
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
            self._drop(entry_id)


def create_http_cache_storage(settings: Any) -> AsyncBaseStorage:
    """The HTTP cache storage for `settings.cache_backend_url`, under `KeySpace.http_cache()`."""
    prefix = KeySpace.for_cache(settings).http_cache()
    ttl = float(settings.cache_default_ttl)
    if is_redis(settings.cache_backend_url):
        return AsyncRedisStorage(
            get_redis(settings.cache_backend_url),
            default_ttl=ttl,
            max_stream_size=DEFAULT_MAX_STREAM_SIZE,
            key_prefix=prefix,
        )
    return AsyncMemoryStorage(prefix, default_ttl=ttl)
