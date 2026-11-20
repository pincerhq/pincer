"""The opt-in HTTP response cache (pincer.core.cache): storages, middleware and API wiring."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from hishel import AsyncRedisStorage, Headers, Request, Response

from pincer.core.cache.middleware import HTTPCacheMiddleware, cache_control, cached_paths
from pincer.core.cache.storage import AsyncMemoryStorage, create_http_cache_storage

# ── AsyncMemoryStorage ───────────────────────────────────────────────────────


async def _body(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _store(storage: AsyncMemoryStorage, key: str, *chunks: bytes) -> bytes:
    entry = await storage.create_entry(
        Request("GET", f"http://t/{key}"), Response(200, Headers({"x": "1"}), _body(*chunks)), key
    )
    assert entry.response.stream is not None
    return b"".join([c async for c in entry.response.stream])  # type: ignore[union-attr]


async def _read(storage: AsyncMemoryStorage, key: str) -> list[bytes]:
    bodies = []
    for entry in await storage.get_entries(key):
        bodies.append(b"".join([c async for c in entry.response.stream]))  # type: ignore[union-attr]
    return bodies


async def test_memory_storage_stores_after_the_body_is_read() -> None:
    storage = AsyncMemoryStorage("p:pincer:kv:httpcache")
    entry = await storage.create_entry(Request("GET", "http://t/a"), Response(200, stream=_body(b"a", b"b")), "k")
    assert await storage.get_entries("k") == []  # not visible until the stream is consumed
    assert b"".join([c async for c in entry.response.stream]) == b"ab"  # type: ignore[union-attr]
    assert await _read(storage, "k") == [b"ab"]
    assert storage.index_key("k") == "p:pincer:kv:httpcache:idx:k"


async def test_memory_storage_expires_by_ttl_and_tti() -> None:
    now = [1000.0]
    storage = AsyncMemoryStorage("p", default_ttl=300, clock=lambda: now[0])
    await _store(storage, "k", b"x")
    now[0] += 299
    assert await _read(storage, "k") == [b"x"]
    now[0] += 2
    assert await _read(storage, "k") == []

    idle = AsyncMemoryStorage("p", default_tti=10, clock=lambda: now[0])
    await _store(idle, "k", b"x")
    now[0] += 9
    assert await _read(idle, "k") == [b"x"]  # restarts the idle timer
    now[0] += 9
    assert await _read(idle, "k") == [b"x"]
    now[0] += 11
    assert await _read(idle, "k") == []


async def test_memory_storage_is_bounded_and_skips_large_bodies() -> None:
    storage = AsyncMemoryStorage("p", max_entries=2, max_stream_size=4)
    for key in ("a", "b", "c"):
        await _store(storage, key, b"1")
    assert storage.size == 2
    assert await _read(storage, "a") == []  # least recently used, evicted
    assert await _store(storage, "big", b"12", b"345") == b"12345"  # still served in full...
    assert await _read(storage, "big") == []  # ...but not stored


async def test_memory_storage_update_and_remove() -> None:
    storage = AsyncMemoryStorage("p")
    await _store(storage, "k", b"x")
    (entry,) = await storage.get_entries("k")
    await storage.update_headers(entry.id, Headers({"etag": "v2"}))
    (updated,) = await storage.get_entries("k")
    assert updated.response.headers.get("etag") == "v2"
    await storage.remove_entry(entry.id)
    await storage.remove_entry(uuid.uuid4())  # unknown ids are ignored
    assert await storage.get_entries("k") == []


def test_create_http_cache_storage_picks_backend_and_prefix() -> None:
    settings = SimpleNamespace(
        cache_backend_url="memory://",
        cache_prefix="{environment}:{service}",
        environment="production",
        cache_default_ttl=300,
    )
    memory = create_http_cache_storage(settings)
    assert isinstance(memory, AsyncMemoryStorage)
    assert memory.prefix == "production:pincer:kv:httpcache"
    assert memory.default_ttl == 300

    settings.cache_backend_url = "redis://localhost:6379/0"
    assert isinstance(create_http_cache_storage(settings), AsyncRedisStorage)


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


async def test_redis_storage_keys_carry_the_prefix() -> None:
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis()
    # hishel's AsyncRedisStorage on the redis-py 7 that repid pins (pyproject `redis` extra).
    storage = AsyncRedisStorage(client, default_ttl=300, key_prefix="production:pincer:kv:httpcache")
    entry = await storage.create_entry(Request("GET", "http://t/a"), Response(200, stream=_body(b"hello")), "k")
    assert b"".join([c async for c in entry.response.stream]) == b"hello"  # type: ignore[union-attr]
    (stored,) = await storage.get_entries("k")
    assert b"".join([c async for c in stored.response.stream]) == b"hello"  # type: ignore[union-attr]
    keys = {k.decode() for k in await client.keys("*")}
    assert keys and all(k.startswith("production:pincer:kv:httpcache:") for k in keys)
    ttls = [await client.pttl(k) for k in keys]
    assert all(0 < ttl <= 480_000 for ttl in ttls)  # TTL + soft-delete window
    await client.aclose()


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
