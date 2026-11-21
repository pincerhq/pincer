"""The key-value store behind `@cache_response` (pincer.core.cache.kv): both modes, both backends."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pincer.core.cache import backend
from pincer.core.cache.kv import (
    AsyncMemoryKeyValueStore,
    AsyncRedisKeyValueStore,
    MemoryKeyValueStore,
    RedisKeyValueStore,
    create_kv_stores,
)

fakeredis = pytest.importorskip("fakeredis")


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@pytest.fixture(params=["sync-memory", "async-memory", "sync-redis", "async-redis"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    mode, kind = request.param.split("-")
    if kind == "memory":
        memory = MemoryKeyValueStore()
        yield memory if mode == "sync" else AsyncMemoryKeyValueStore(memory)
        return
    if mode == "sync":
        client = fakeredis.FakeRedis()
        yield RedisKeyValueStore(client)
        client.close()
    else:
        aclient = fakeredis.FakeAsyncRedis()
        yield AsyncRedisKeyValueStore(aclient)
        await aclient.aclose()


async def test_set_get_delete(store: Any) -> None:
    assert await _maybe(store.get("p:kv:a")) is None
    await _maybe(store.set("p:kv:a", b"1", 60))
    assert await _maybe(store.get("p:kv:a")) == b"1"
    await _maybe(store.delete("p:kv:a"))
    assert await _maybe(store.get("p:kv:a")) is None


async def test_delete_prefix_only_takes_that_prefix(store: Any) -> None:
    for key in ("p:kv:apicache:r:alice:1", "p:kv:apicache:r:alice:2", "p:kv:apicache:r:bob:1", "p:kv:apicache:rx:a:1"):
        await _maybe(store.set(key, b"x", 60))
    assert await _maybe(store.delete_prefix("p:kv:apicache:r:alice:")) == 2
    assert await _maybe(store.delete_prefix("p:kv:apicache:r:")) == 1
    assert await _maybe(store.get("p:kv:apicache:rx:a:1")) == b"x"


async def test_delete_prefix_escapes_glob_characters(store: Any) -> None:
    await _maybe(store.set("p:kv:a*b:1", b"x", 60))
    await _maybe(store.set("p:kv:aXb:1", b"x", 60))
    assert await _maybe(store.delete_prefix("p:kv:a*b:")) == 1
    assert await _maybe(store.get("p:kv:aXb:1")) == b"x"


def test_memory_ttl_and_bound() -> None:
    now = [100.0]
    memory = MemoryKeyValueStore(max_entries=2, clock=lambda: now[0])
    memory.set("a", b"1", 10)
    now[0] += 11
    assert memory.get("a") is None
    for key in ("b", "c", "d"):
        memory.set(key, b"1")
    assert memory.size == 2
    assert memory.get("b") is None  # least recently used, evicted


async def test_redis_sets_a_ttl() -> None:
    client = fakeredis.FakeRedis()
    RedisKeyValueStore(client).set("p:kv:a", b"1", 60)
    assert 0 < client.pttl("p:kv:a") <= 60_000
    client.close()


async def test_create_kv_stores_share_data(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_memory, async_memory = create_kv_stores("memory://")
    sync_memory.set("k", b"v")
    assert await async_memory.get("k") == b"v"

    server = fakeredis.FakeServer()
    monkeypatch.setattr(backend, "get_redis", lambda url: fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(backend, "get_async_redis", lambda url: fakeredis.FakeAsyncRedis(server=server))
    sync_redis, async_redis = create_kv_stores("redis://localhost:6379/0")
    assert isinstance(sync_redis, RedisKeyValueStore)
    await async_redis.set("k", b"v")
    assert sync_redis.get("k") == b"v"
