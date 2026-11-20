"""The HTTP caches (pincer.core.cache): storages in both modes, middleware, cached clients, API wiring."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from hishel import AsyncRedisStorage, Headers, RedisStorage, Request, Response

from pincer.core.cache.client import (
    async_cached_client,
    cache_key,
    cached_client,
    client_cache_prefix,
    reset_client_storages,
)
from pincer.core.cache.middleware import HTTPCacheMiddleware, cache_control, cached_paths
from pincer.core.cache.storage import (
    AsyncMemoryStorage,
    MemoryStorage,
    create_async_http_cache_storage,
    create_http_cache_storage,
)

fakeredis = pytest.importorskip("fakeredis")
MODES = ["sync", "async"]

# ── Memory storages, both modes ──────────────────────────────────────────────


def _memory(mode: str, prefix: str = "p", **kwargs: Any) -> MemoryStorage | AsyncMemoryStorage:
    return MemoryStorage(prefix, **kwargs) if mode == "sync" else AsyncMemoryStorage(prefix, **kwargs)


def _body(storage: Any, *chunks: bytes) -> Iterator[bytes] | AsyncIterator[bytes]:
    if isinstance(storage, (MemoryStorage, RedisStorage)):
        return iter(chunks)

    async def agen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return agen()


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _drain(stream: Any) -> bytes:
    if isinstance(stream, AsyncIterator):
        return b"".join([c async for c in stream])
    return b"".join(stream)


async def _create(storage: Any, key: str, *chunks: bytes) -> Any:
    response = Response(200, Headers({"x": "1"}), _body(storage, *chunks))
    return await _maybe(storage.create_entry(Request("GET", f"http://t/{key}"), response, key))


async def _store(storage: Any, key: str, *chunks: bytes) -> bytes:
    return await _drain((await _create(storage, key, *chunks)).response.stream)


async def _read(storage: Any, key: str) -> list[bytes]:
    return [await _drain(entry.response.stream) for entry in await _maybe(storage.get_entries(key))]


@pytest.mark.parametrize("mode", MODES)
async def test_memory_storage_stores_after_the_body_is_read(mode: str) -> None:
    storage = _memory(mode, "p:pincer:kv:httpcache")
    entry = await _create(storage, "k", b"a", b"b")
    assert await _maybe(storage.get_entries("k")) == []  # not visible until the stream is consumed
    assert await _drain(entry.response.stream) == b"ab"
    assert await _read(storage, "k") == [b"ab"]
    assert storage.index_key("k") == "p:pincer:kv:httpcache:idx:k"


@pytest.mark.parametrize("mode", MODES)
async def test_memory_storage_expires_by_ttl_and_tti(mode: str) -> None:
    now = [1000.0]
    storage = _memory(mode, default_ttl=300, clock=lambda: now[0])
    await _store(storage, "k", b"x")
    now[0] += 299
    assert await _read(storage, "k") == [b"x"]
    now[0] += 2
    assert await _read(storage, "k") == []

    idle = _memory(mode, default_tti=10, clock=lambda: now[0])
    await _store(idle, "k", b"x")
    now[0] += 9
    assert await _read(idle, "k") == [b"x"]  # restarts the idle timer
    now[0] += 9
    assert await _read(idle, "k") == [b"x"]
    now[0] += 11
    assert await _read(idle, "k") == []


@pytest.mark.parametrize("mode", MODES)
async def test_memory_storage_is_bounded_and_skips_large_bodies(mode: str) -> None:
    storage = _memory(mode, max_entries=2, max_stream_size=4)
    for key in ("a", "b", "c"):
        await _store(storage, key, b"1")
    assert storage.size == 2
    assert await _read(storage, "a") == []  # least recently used, evicted
    assert await _store(storage, "big", b"12", b"345") == b"12345"  # still served in full...
    assert await _read(storage, "big") == []  # ...but not stored


@pytest.mark.parametrize("mode", MODES)
async def test_memory_storage_update_and_remove(mode: str) -> None:
    storage = _memory(mode)
    await _store(storage, "k", b"x")
    (entry,) = await _maybe(storage.get_entries("k"))
    await _maybe(storage.update_headers(entry.id, Headers({"etag": "v2"})))
    (updated,) = await _maybe(storage.get_entries("k"))
    assert updated.response.headers.get("etag") == "v2"
    await _maybe(storage.remove_entry(entry.id))
    await _maybe(storage.remove_entry(uuid.uuid4()))  # unknown ids are ignored
    assert await _maybe(storage.get_entries("k")) == []


@pytest.mark.parametrize("mode", MODES)
def test_empty_memory_storage_is_truthy(mode: str) -> None:
    """hishel does `storage or SqliteStorage()`: an empty storage must not read as "none given"."""
    assert _memory(mode)


@pytest.mark.parametrize("mode", MODES)
async def test_redis_storage_keys_carry_the_prefix(mode: str) -> None:
    # hishel's Redis storages on the redis-py 7 that repid pins (pyproject `redis` extra).
    client = fakeredis.FakeRedis() if mode == "sync" else fakeredis.FakeAsyncRedis()
    storage_cls = RedisStorage if mode == "sync" else AsyncRedisStorage
    storage = storage_cls(client, default_ttl=300, key_prefix="production:pincer:kv:httpcache")
    assert await _store(storage, "k", b"hello") == b"hello"
    assert await _read(storage, "k") == [b"hello"]
    keys = {k.decode() for k in await _maybe(client.keys("*"))}
    assert keys and all(k.startswith("production:pincer:kv:httpcache:") for k in keys)
    ttls = [await _maybe(client.pttl(k)) for k in keys]
    assert all(0 < ttl <= 480_000 for ttl in ttls)  # TTL + soft-delete window
    await _maybe(client.aclose() if mode == "async" else client.close())


def _settings(url: str = "memory://") -> SimpleNamespace:
    return SimpleNamespace(
        cache_backend_url=url, cache_prefix="{environment}:{service}", environment="production", cache_default_ttl=300
    )


def test_storage_factories_pick_backend_mode_and_prefix() -> None:
    settings = _settings()
    sync_memory, async_memory = create_http_cache_storage(settings), create_async_http_cache_storage(settings)
    assert isinstance(sync_memory, MemoryStorage)
    assert isinstance(async_memory, AsyncMemoryStorage)
    assert sync_memory.prefix == async_memory.prefix == "production:pincer:kv:httpcache"
    assert sync_memory.default_ttl == async_memory.default_ttl == 300
    assert create_http_cache_storage(settings, prefix="x:pincer:kv:other").prefix == "x:pincer:kv:other"

    settings.cache_backend_url = "redis://localhost:6379/0"
    assert isinstance(create_http_cache_storage(settings), RedisStorage)
    assert isinstance(create_async_http_cache_storage(settings), AsyncRedisStorage)


# ── Cached outbound clients, both modes ──────────────────────────────────────


class _Upstream:
    """A counting stand-in for the remote API."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if request.url.path == "/missing":
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(200, json={"n": self.calls, "path": request.url.path})


@pytest.fixture(autouse=True)
def _fresh_client_storages() -> Iterator[None]:
    reset_client_storages()
    yield
    reset_client_storages()


async def _get(mode: str, upstream: _Upstream, path: str, *, key: str = "k1", method: str = "GET") -> httpx.Response:
    settings = _settings()
    headers = {"xi-api-key": key}
    if mode == "sync":
        with cached_client(
            settings,
            "test-api",
            key_headers=("xi-api-key",),
            transport=httpx.MockTransport(upstream),
            base_url="https://api.test",
        ) as client:
            response = client.request(method, path, headers=headers)
            response.read()
            return response
    async with async_cached_client(
        settings,
        "test-api",
        key_headers=("xi-api-key",),
        transport=httpx.MockTransport(upstream),
        base_url="https://api.test",
    ) as client:
        response = await client.request(method, path, headers=headers)
        await response.aread()
        return response


@pytest.mark.parametrize("mode", MODES)
async def test_cached_client_reuses_a_get_across_clients(mode: str) -> None:
    upstream = _Upstream()
    first = await _get(mode, upstream, "/v2/voices")
    second = await _get(mode, upstream, "/v2/voices")  # a new client, the same process-wide storage
    assert first.json() == second.json() == {"n": 1, "path": "/v2/voices"}
    assert upstream.calls == 1
    assert second.extensions["caching"]["from_cache"] is True


@pytest.mark.parametrize("mode", MODES)
async def test_cached_client_keys_by_credential(mode: str) -> None:
    upstream = _Upstream()
    await _get(mode, upstream, "/v2/voices", key="account-a")
    other = await _get(mode, upstream, "/v2/voices", key="account-b")
    assert other.json()["n"] == 2  # another account never gets account-a's answer
    assert upstream.calls == 2


@pytest.mark.parametrize("mode", MODES)
async def test_cached_client_skips_errors_and_posts(mode: str) -> None:
    upstream = _Upstream()
    for _ in range(2):
        assert (await _get(mode, upstream, "/missing")).status_code == 404
        await _get(mode, upstream, "/v1/text-to-speech/x", method="POST")
    assert upstream.calls == 4


async def test_sync_and_async_clients_share_a_redis_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    server = fakeredis.FakeServer()
    monkeypatch.setattr("pincer.core.cache.storage.get_redis", lambda url: fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(
        "pincer.core.cache.storage.get_async_redis", lambda url: fakeredis.FakeAsyncRedis(server=server)
    )
    settings = _settings("redis://localhost:6379/0")
    upstream = _Upstream()
    with cached_client(
        settings, "shared", transport=httpx.MockTransport(upstream), base_url="https://api.test"
    ) as client:
        client.get("/thing").read()
    async with async_cached_client(
        settings, "shared", transport=httpx.MockTransport(upstream), base_url="https://api.test"
    ) as aclient:
        response = await aclient.get("/thing")
        await response.aread()
    assert upstream.calls == 1
    assert response.extensions["caching"]["from_cache"] is True
    keys = {k.decode() for k in fakeredis.FakeRedis(server=server).keys("*")}
    assert keys and all(k.startswith("production:pincer:kv:httpclient:shared:") for k in keys)


async def test_closing_a_cached_client_leaves_the_shared_redis_client_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage's Redis client is process-wide (rate limiter, kv store): only `close_all()` closes it."""
    server = fakeredis.FakeServer()
    sync_redis, async_redis = fakeredis.FakeRedis(server=server), fakeredis.FakeAsyncRedis(server=server)
    closed: list[str] = []
    monkeypatch.setattr(sync_redis, "close", lambda: closed.append("sync"))

    async def aclose() -> None:
        closed.append("async")

    monkeypatch.setattr(async_redis, "close", aclose)
    monkeypatch.setattr(async_redis, "aclose", aclose)
    monkeypatch.setattr("pincer.core.cache.storage.get_redis", lambda url: sync_redis)
    monkeypatch.setattr("pincer.core.cache.storage.get_async_redis", lambda url: async_redis)
    settings = _settings("redis://localhost:6379/0")
    upstream = _Upstream()
    with cached_client(settings, "shared", transport=httpx.MockTransport(upstream), base_url="https://api.test") as c:
        c.get("/thing").read()
    async with async_cached_client(
        settings, "shared", transport=httpx.MockTransport(upstream), base_url="https://api.test"
    ) as ac:
        await (await ac.get("/thing")).aread()
    assert closed == []


def test_client_cache_key_hashes_the_credential() -> None:
    request = httpx.Request("GET", "https://api.test/v2/voices", headers={"xi-api-key": "secret"})
    key = cache_key(request, ("xi-api-key",))
    assert "secret" not in key
    assert key != cache_key(
        httpx.Request("GET", "https://api.test/v2/voices", headers={"xi-api-key": "other"}), ("xi-api-key",)
    )
    assert client_cache_prefix(_settings(), "eleven labs") == "production:pincer:kv:httpclient:eleven-labs"


# ── HTTPCacheMiddleware on a bare app ────────────────────────────────────────


def _app(storage: Any) -> tuple[FastAPI, dict[str, int]]:
    calls = {"public": 0, "private": 0, "plain": 0, "stream": 0}
    router = APIRouter(prefix="/api/things")

    @router.get("/public", dependencies=[cache_control(max_age=60, public=True)])
    async def public() -> dict[str, int]:
        calls["public"] += 1
        return {"n": calls["public"]}

    @router.get("/private", dependencies=[cache_control(max_age=60, private=True)])
    async def private() -> dict[str, int]:
        calls["private"] += 1
        return {"n": calls["private"]}

    @router.get("/plain")
    async def plain() -> dict[str, int]:
        calls["plain"] += 1
        return {"n": calls["plain"]}

    @router.post("/public")
    async def change() -> dict[str, str]:
        return {"ok": "yes"}

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        calls["stream"] += 1

        async def chunks() -> AsyncIterator[bytes]:
            yield b"data: 1\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(HTTPCacheMiddleware, storage=storage)
    return app, calls


def test_only_opted_in_routes_are_matched() -> None:
    app, _ = _app(AsyncMemoryStorage("p"))
    assert [p.pattern for p in cached_paths(app.routes)] == ["^/api/things/public$", "^/api/things/private$"]


def test_public_response_is_served_from_cache() -> None:
    app, calls = _app(AsyncMemoryStorage("p"))
    client = TestClient(app)
    auth = {"Authorization": "Bearer anyone"}
    first = client.get("/api/things/public", headers=auth)
    second = client.get("/api/things/public", headers=auth)
    assert first.json() == second.json() == {"n": 1}
    assert calls["public"] == 1
    assert "public" in first.headers["cache-control"]
    assert client.get("/api/things/public?page=2").json() == {"n": 2}  # the query is part of the key


def test_private_and_unmarked_responses_are_not_cached() -> None:
    app, calls = _app(AsyncMemoryStorage("p"))
    client = TestClient(app)
    for _ in range(2):
        client.get("/api/things/private")
        client.get("/api/things/plain")
        client.get("/api/things/stream")
    assert calls == {"public": 0, "private": 2, "plain": 2, "stream": 2}


def test_post_passes_through() -> None:
    storage = AsyncMemoryStorage("p")
    app, _ = _app(storage)
    assert TestClient(app).post("/api/things/public").json() == {"ok": "yes"}
    assert storage.size == 0


# ── API wiring ───────────────────────────────────────────────────────────────


def test_ops_digest_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    get_settings_relaxed.cache_clear()
    calls: list[int] = []

    async def build_digest(settings: Any) -> str:
        calls.append(1)
        return f"digest {len(calls)}"

    monkeypatch.setattr("pincer.observability.digest.build_digest", build_digest)
    client = TestClient(create_app())
    assert client.get("/api/ops/digest").json() == {"digest": "digest 1"}
    assert client.get("/api/ops/digest").json() == {"digest": "digest 1"}
    assert len(calls) == 1
    get_settings_relaxed.cache_clear()
