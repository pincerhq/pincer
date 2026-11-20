"""Rate-limit storages (memory and Redis via fakeredis) and the fail-open limiter."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import Limiter, MemoryStorage, Rate, RedisStorage, SlidingWindowStrategy, TokenBucketStrategy
from pincer.core.ratelimit.storage import RateLimitStorage, create_storage

KEYS = KeySpace("test:pincer")


@pytest.fixture
async def fake_redis() -> AsyncIterator[Any]:
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture(params=["memory", "redis"])
async def storage(request: pytest.FixtureRequest, fake_redis: Any) -> MemoryStorage | RedisStorage:
    return MemoryStorage() if request.param == "memory" else RedisStorage(fake_redis)


@pytest.mark.parametrize("strategy", [SlidingWindowStrategy(), TokenBucketStrategy()], ids=lambda s: s.name)
async def test_storage_allows_then_denies(storage: MemoryStorage | RedisStorage, strategy: Any) -> None:
    rate = Rate(3, 60)
    results = [await storage.hit("test:pincer:kv:ratelimit:ip:1", strategy, rate) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[0].remaining == 2
    assert results[-1].remaining == 0
    assert results[-1].retry_after > 0
    assert all(r.limit == 3 for r in results)


async def test_storage_buckets_are_independent(storage: MemoryStorage | RedisStorage) -> None:
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    assert (await storage.hit("a", strategy, rate)).allowed
    assert not (await storage.hit("a", strategy, rate)).allowed
    assert (await storage.hit("b", strategy, rate)).allowed


async def test_storage_reset(storage: MemoryStorage | RedisStorage) -> None:
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    await storage.hit("a", strategy, rate)
    await storage.reset("a")
    assert (await storage.hit("a", strategy, rate)).allowed


@pytest.mark.parametrize("strategy", [SlidingWindowStrategy(), TokenBucketStrategy()], ids=lambda s: s.name)
async def test_redis_bucket_gets_a_ttl_under_the_prefix(fake_redis: Any, strategy: Any) -> None:
    limiter = Limiter(RedisStorage(fake_redis), Rate(5, 60), KEYS, "user", strategy)
    await limiter.hit("018f-user")
    keys = [k.decode() for k in await fake_redis.keys("*")]
    assert keys == ["test:pincer:kv:ratelimit:user:018f-user"]
    ttl_ms = await fake_redis.pttl(keys[0])
    assert 0 < ttl_ms <= 120_000


async def test_memory_storage_expires_idle_buckets() -> None:
    now = [1000.0]
    storage = MemoryStorage(clock=lambda: now[0])
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    await storage.hit("a", strategy, rate)
    assert not (await storage.hit("a", strategy, rate)).allowed
    now[0] += 200  # past the bucket's TTL and the sweep interval
    assert (await storage.hit("b", strategy, rate)).allowed
    assert len(storage) == 1  # "a" was swept


async def test_memory_storage_is_bounded() -> None:
    storage = MemoryStorage(max_keys=10)
    for i in range(50):
        await storage.hit(f"k{i}", SlidingWindowStrategy(), Rate(5, 60))
    assert len(storage) <= 11


async def test_limiter_names_buckets_by_scope_and_sanitises_ipv6() -> None:
    storage = MemoryStorage()
    limiter = Limiter(storage, Rate(1, 60), KEYS, "ip")
    assert limiter.key("2001:db8::1") == "test:pincer:kv:ratelimit:ip:2001-db8--1"
    assert (await limiter.hit("2001:db8::1")).allowed
    assert not (await limiter.hit("2001:0db8::0001")).allowed  # same address, same bucket


class _BrokenStorage(RateLimitStorage):
    def __init__(self) -> None:
        self.fail = True

    async def hit(self, key: str, strategy: Any, rate: Rate, cost: int = 1) -> Any:
        if self.fail:
            raise ConnectionError("redis down")
        return await MemoryStorage().hit(key, strategy, rate, cost)

    async def reset(self, key: str) -> None:
        return None


async def test_limiter_fails_open_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    broken = _BrokenStorage()
    limiter = Limiter(broken, Rate(1, 60), KEYS, "ip")
    with caplog.at_level(logging.DEBUG, logger="pincer.core.ratelimit.limiter"):
        first = await limiter.hit("1.2.3.4")
        second = await limiter.hit("1.2.3.4")
        broken.fail = False
        await limiter.hit("1.2.3.4")
    assert first.allowed and second.allowed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "allowing requests" in warnings[0].getMessage()
    assert any("recovered" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)


def test_create_storage_picks_the_backend() -> None:
    assert isinstance(create_storage("memory://"), MemoryStorage)
    assert isinstance(create_storage("redis://localhost:6379/0"), RedisStorage)
