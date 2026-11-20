"""Cached outbound HTTP: httpx clients (sync and async) whose GET/HEAD responses go through hishel.

    with cached_client(settings, "elevenlabs", key_headers=("xi-api-key",), base_url=API_BASE) as client:
        client.get("/v2/voices", headers={"xi-api-key": key})

- Storage is the cache backend (`PINCER_CACHE_BACKEND_URL`) under
  `<prefix>:kv:httpclient:<namespace>`, apart from the server-side cache.
  With `redis://` every process shares it; with `memory://` one storage per
  namespace lives for the whole process, so short-lived clients still hit it.
- Third-party APIs rarely send caching headers, so this is hishel's non-RFC
  mode: a 2xx GET/HEAD is kept for `cache_default_ttl` seconds. Other methods
  and other statuses always go to the network.
- The cache key is the method and URL, plus a hash of the `key_headers`
  values (the credential). Two accounts never share an entry, and no
  credential is stored in a key.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Any

import httpx
from hishel.httpx import AsyncCacheTransport, CacheTransport

from pincer.config.dsn import backend_name
from pincer.core.cache.keys import KeySpace, key_part
from pincer.core.cache.storage import create_async_http_cache_storage, create_http_cache_storage

if TYPE_CHECKING:
    from hishel import AsyncBaseStorage, BaseStorage

_storages: dict[tuple[str, str, str], BaseStorage] = {}
_async_storages: dict[tuple[str, str, str], AsyncBaseStorage] = {}
_lock = threading.Lock()


def client_cache_prefix(settings: Any, namespace: str) -> str:
    """`<prefix>:kv:httpclient:<namespace>`."""
    return KeySpace.for_cache(settings).kv("httpclient", key_part(namespace))


def cache_key(request: httpx.Request, key_headers: tuple[str, ...]) -> str:
    """hishel's non-RFC key (method + URL), extended with a hash of the `key_headers` values."""
    identity = f"{request.method} {request.url}"
    for name in key_headers:
        identity += "\n" + hashlib.sha256(request.headers.get(name, "").encode()).hexdigest()
    return hashlib.sha256(identity.encode()).hexdigest()


def _with_key(request: httpx.Request, key_headers: tuple[str, ...]) -> None:
    if key_headers and "key" not in request.extensions.get("caching", {}):
        request.extensions["caching"] = {
            **request.extensions.get("caching", {}),
            "key": cache_key(request, key_headers),
        }


class _KeyedTransport(httpx.BaseTransport):
    def __init__(self, inner: CacheTransport, key_headers: tuple[str, ...]) -> None:
        self._inner = inner
        self._key_headers = key_headers

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _with_key(request, self._key_headers)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class _AsyncKeyedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: AsyncCacheTransport, key_headers: tuple[str, ...]) -> None:
        self._inner = inner
        self._key_headers = key_headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _with_key(request, self._key_headers)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _storage_key(settings: Any, namespace: str) -> tuple[str, str, str]:
    return (
        backend_name(settings.cache_backend_url),
        str(settings.cache_backend_url),
        client_cache_prefix(settings, namespace),
    )


def client_storage(settings: Any, namespace: str) -> BaseStorage:
    """The process-wide sync storage for *namespace*."""
    key = _storage_key(settings, namespace)
    with _lock:
        storage = _storages.get(key)
        if storage is None:
            storage = create_http_cache_storage(settings, prefix=key[2])
            _storages[key] = storage
    return storage


def async_client_storage(settings: Any, namespace: str) -> AsyncBaseStorage:
    """The process-wide async storage for *namespace*."""
    key = _storage_key(settings, namespace)
    with _lock:
        storage = _async_storages.get(key)
        if storage is None:
            storage = create_async_http_cache_storage(settings, prefix=key[2])
            _async_storages[key] = storage
    return storage


def cached_client(
    settings: Any,
    namespace: str,
    *,
    key_headers: tuple[str, ...] = (),
    transport: httpx.BaseTransport | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """A sync `httpx.Client` whose GET/HEAD 2xx responses are cached (see the module docstring).

    *transport* is the network transport under the cache (default
    `httpx.HTTPTransport()`); the other keyword arguments go to `httpx.Client`.
    """
    inner = CacheTransport(transport or httpx.HTTPTransport(), storage=client_storage(settings, namespace), rfc=False)
    return httpx.Client(transport=_KeyedTransport(inner, key_headers), **kwargs)


def async_cached_client(
    settings: Any,
    namespace: str,
    *,
    key_headers: tuple[str, ...] = (),
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` whose GET/HEAD 2xx responses are cached (see the module docstring)."""
    inner = AsyncCacheTransport(
        transport or httpx.AsyncHTTPTransport(), storage=async_client_storage(settings, namespace), rfc=False
    )
    return httpx.AsyncClient(transport=_AsyncKeyedTransport(inner, key_headers), **kwargs)


def reset_client_storages() -> None:
    """Forget the process-wide storages (tests, or after the backend settings change)."""
    with _lock:
        _storages.clear()
        _async_storages.clear()
