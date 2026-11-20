"""Rate-limit storages and limiters in both modes (sync/async) on both backends (memory/fakeredis).

The `limiter` fixture hands every test one `hit`/`reset` API (awaitable either
way), so each behaviour is checked in all four combinations.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import (
    AsyncLimiter,
    AsyncMemoryStorage,
    AsyncRateLimitStorage,
    AsyncRedisStorage,
    Limiter,
    MemoryStorage,
    Rate,
    RateLimitStorage,
    RedisStorage,
    SlidingWindowStrategy,
    Strategy,
    TokenBucketStrategy,
    create_async_storage,
    create_storage,
)

KEYS = KeySpace("test:pincer")
fakeredis = pytest.importorskip("fakeredis")


@dataclass
class Harness:
    """A storage in one mode, driven through the same awaitable calls."""

    mode: str
    storage: RateLimitStorage | AsyncRateLimitStorage
    redis: Any | None

    async def hit(self, key: str, strategy: Strategy, rate: Rate) -> Any:
        return await _maybe(self.storage.hit(key, strategy, rate))

    async def reset(self, key: str) -> None:
        await _maybe(self.storage.reset(key))

    def limiter(self, rate: Rate, scope: str, strategy: Strategy | None = None) -> Limiter | AsyncLimiter:
        if isinstance(self.storage, RateLimitStorage):
            return Limiter(self.storage, rate, KEYS, scope, strategy)
        return AsyncLimiter(self.storage, rate, KEYS, scope, strategy)

    async def keys(self) -> list[str]:
        assert self.redis is not None
        return [k.decode() for k in await _maybe(self.redis.keys("*"))]

    async def pttl(self, key: str) -> int:
        assert self.redis is not None
        return int(await _maybe(self.redis.pttl(key)))


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@pytest.fixture(params=["sync-memory", "async-memory", "sync-redis", "async-redis"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    mode, backend = request.param.split("-")
    if backend == "memory":
        yield Harness(mode, MemoryStorage() if mode == "sync" else AsyncMemoryStorage(), None)
        return
    if mode == "sync":
        client = fakeredis.FakeRedis()
        yield Harness(mode, RedisStorage(client), client)
        client.close()
    else:
        client = fakeredis.FakeAsyncRedis()
        yield Harness(mode, AsyncRedisStorage(client), client)
        await client.aclose()


STRATEGIES = [SlidingWindowStrategy(), TokenBucketStrategy()]


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_storage_allows_then_denies(harness: Harness, strategy: Strategy) -> None:
    rate = Rate(3, 60)
    results = [await harness.hit("test:pincer:kv:ratelimit:ip:1", strategy, rate) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[0].remaining == 2
    assert results[-1].remaining == 0
    assert results[-1].retry_after > 0
    assert all(r.limit == 3 for r in results)


async def test_storage_buckets_are_independent(harness: Harness) -> None:
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    assert (await harness.hit("a", strategy, rate)).allowed
    assert not (await harness.hit("a", strategy, rate)).allowed
    assert (await harness.hit("b", strategy, rate)).allowed


async def test_storage_reset(harness: Harness) -> None:
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    await harness.hit("a", strategy, rate)
    await harness.reset("a")
    assert (await harness.hit("a", strategy, rate)).allowed


async def test_limiter_counts_and_resets(harness: Harness) -> None:
    limiter = harness.limiter(Rate(2, 60), "user")
    results = [await _maybe(limiter.hit("alice")) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    await _maybe(limiter.reset("alice"))
    assert (await _maybe(limiter.hit("alice"))).allowed


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_redis_bucket_gets_a_ttl_under_the_prefix(harness: Harness, strategy: Strategy) -> None:
    if harness.redis is None:
        pytest.skip("Redis only")
    limiter = harness.limiter(Rate(5, 60), "user", strategy)
    await _maybe(limiter.hit("018f-user"))
    keys = await harness.keys()
    assert keys == ["test:pincer:kv:ratelimit:user:018f-user"]
    assert 0 < await harness.pttl(keys[0]) <= 120_000


async def test_sync_and_async_limiters_share_redis_buckets() -> None:
    server = fakeredis.FakeServer()
    sync_client = fakeredis.FakeRedis(server=server)
    async_client = fakeredis.FakeAsyncRedis(server=server)
    rate = Rate(3, 60)
    sync_limiter = Limiter(RedisStorage(sync_client), rate, KEYS, "ip")
    async_limiter = AsyncLimiter(AsyncRedisStorage(async_client), rate, KEYS, "ip")
    assert sync_limiter.key("10.0.0.1") == async_limiter.key("10.0.0.1")
    assert sync_limiter.hit("10.0.0.1").allowed
    assert (await async_limiter.hit("10.0.0.1")).allowed
    assert sync_limiter.hit("10.0.0.1").allowed
    assert not (await async_limiter.hit("10.0.0.1")).allowed  # the fourth, whichever mode sends it
    sync_client.close()
    await async_client.aclose()


@pytest.mark.parametrize("storage_cls", [MemoryStorage, AsyncMemoryStorage])
async def test_memory_storage_expires_idle_buckets(storage_cls: Callable[..., Any]) -> None:
    now = [1000.0]
    storage = storage_cls(clock=lambda: now[0])
    rate, strategy = Rate(1, 60), SlidingWindowStrategy()
    await _maybe(storage.hit("a", strategy, rate))
    assert not (await _maybe(storage.hit("a", strategy, rate))).allowed
    now[0] += 200  # past the bucket's TTL and the sweep interval
    assert (await _maybe(storage.hit("b", strategy, rate))).allowed
    assert storage.size == 1  # "a" was swept


@pytest.mark.parametrize("storage_cls", [MemoryStorage, AsyncMemoryStorage])
async def test_memory_storage_is_bounded(storage_cls: Callable[..., Any]) -> None:
    storage = storage_cls(max_keys=10)
    for i in range(50):
        await _maybe(storage.hit(f"k{i}", SlidingWindowStrategy(), Rate(5, 60)))
    assert storage.size <= 11


def test_sync_memory_storage_is_thread_safe() -> None:
    limiter = Limiter(MemoryStorage(), Rate(100, 60), KEYS, "ip", TokenBucketStrategy())
    allowed: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        mine = [limiter.hit("shared").allowed for _ in range(50)]
        with lock:
            allowed.extend(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(allowed) == 400
    assert sum(allowed) == 100  # exactly the limit, never more under contention


async def test_limiter_names_buckets_by_scope_and_sanitises_ipv6(harness: Harness) -> None:
    limiter = harness.limiter(Rate(1, 60), "ip")
    assert limiter.key("2001:db8::1") == "test:pincer:kv:ratelimit:ip:2001-db8--1"
    assert (await _maybe(limiter.hit("2001:db8::1"))).allowed
    assert not (await _maybe(limiter.hit("2001:0db8::0001"))).allowed  # same address, same bucket


class _Broken:
    fail = True

    def _hit(self, key: str, strategy: Strategy, rate: Rate, cost: int) -> Any:
        if self.fail:
            raise ConnectionError("redis down")
        return strategy.apply(None, 0.0, rate, cost)[1]


class _BrokenStorage(_Broken, RateLimitStorage):
    def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1, *, dry_run: bool = False) -> Any:
        return self._hit(key, strategy, rate, cost)

    def reset(self, key: str) -> None:
        return None


class _AsyncBrokenStorage(_Broken, AsyncRateLimitStorage):
    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1, *, dry_run: bool = False) -> Any:
        return self._hit(key, strategy, rate, cost)

    async def reset(self, key: str) -> None:
        return None


@pytest.mark.parametrize(
    ("storage_cls", "limiter_cls"), [(_BrokenStorage, Limiter), (_AsyncBrokenStorage, AsyncLimiter)]
)
async def test_limiter_fails_open_and_logs_once(
    caplog: pytest.LogCaptureFixture, storage_cls: type[_Broken], limiter_cls: Any
) -> None:
    broken = storage_cls()
    limiter = limiter_cls(broken, Rate(1, 60), KEYS, "ip")
    with caplog.at_level(logging.DEBUG, logger="pincer.core.ratelimit.limiter"):
        first = await _maybe(limiter.hit("1.2.3.4"))
        second = await _maybe(limiter.hit("1.2.3.4"))
        broken.fail = False
        await _maybe(limiter.hit("1.2.3.4"))
    assert first.allowed and second.allowed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "allowing requests" in warnings[0].getMessage()
    assert any("recovered" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)


def test_create_storage_picks_backend_and_mode() -> None:
    assert isinstance(create_storage("memory://"), MemoryStorage)
    assert isinstance(create_storage("redis://localhost:6379/0"), RedisStorage)
    assert isinstance(create_async_storage("memory://"), AsyncMemoryStorage)
    assert isinstance(create_async_storage("redis://localhost:6379/0"), AsyncRedisStorage)


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_check_reports_without_charging(harness: Harness, strategy: Strategy) -> None:
    limiter = harness.limiter(Rate(2, 60), "user", strategy)
    for _ in range(5):
        checked = await _maybe(limiter.check("alice"))
        assert checked.allowed and checked.remaining == 1  # nothing spent by checking
    await _maybe(limiter.hit("alice"))
    await _maybe(limiter.hit("alice"))
    refused = await _maybe(limiter.check("alice"))
    assert not refused.allowed and refused.retry_after > 0


@pytest.mark.parametrize(
    ("storage_cls", "limiter_cls"), [(_BrokenStorage, Limiter), (_AsyncBrokenStorage, AsyncLimiter)]
)
async def test_limiter_can_fail_closed(storage_cls: type[_Broken], limiter_cls: Any) -> None:
    closed = limiter_cls(storage_cls(), Rate(5, 60), KEYS, "demo", fail_open=False)
    for call in (closed.hit, closed.check):
        result = await _maybe(call("x"))
        assert not result.allowed and result.failed and result.retry_after > 0
    opened = limiter_cls(storage_cls(), Rate(5, 60), KEYS, "demo")
    result = await _maybe(opened.hit("x"))
    assert result.allowed and result.failed
