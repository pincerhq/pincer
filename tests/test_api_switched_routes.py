"""Which pincer routes use the reworked rate limits and response cache, and that they behave (issue #404).

See docs/specs/cache-and-rate-limiting-rework.md for why each route is (or isn't) switched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from pincer.core.cache import backend
from pincer.core.cache.decorators import cache_rule
from pincer.core.cache.middleware import cached_paths
from pincer.core.ratelimit.decorators import route_limits

fakeredis = pytest.importorskip("fakeredis")


def _routes() -> dict[tuple[str, str], Any]:
    from pincer.api.server import create_app

    routes = {}
    for context in iter_route_contexts(create_app().routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path:
            for method in route.methods:
                routes[(method, context.path)] = route.endpoint
    return routes


# (method, path) -> (limits as "rate@scope" in source order, cache "ttl@scope" or None)
EXPECTED: dict[tuple[str, str], tuple[list[str], str | None]] = {
    ("POST", "/api/chat/message"): (["chat:token_bucket@user"], None),
    ("POST", "/api/chat/stream"): (["chat:token_bucket@user"], None),
    ("POST", "/api/voice/calls"): (["10/3600s@user"], None),
    ("POST", "/api/voice/calls/scheduled"): (["10/3600s@user"], None),
    ("POST", "/api/voice/listen/{call_sid}/ticket"): (["30/60s@user"], None),
    ("POST", "/api/ops/canary"): (["6/3600s@global"], None),
    ("POST", "/api/skills/rebuild"): (["2/60s@user"], None),
    ("PUT", "/api/identity/me/password"): (["5/3600s@user"], None),
    ("POST", "/api/identity/me/api-key"): (["5/3600s@user"], None),
    ("PUT", "/api/storage/text"): (["60/60s@user"], None),
    ("POST", "/api/storage/upload"): (["60/60s@user"], None),
    ("POST", "/api/storage/folders"): (["60/60s@user"], None),
    ("POST", "/api/storage/move"): (["60/60s@user"], None),
    ("DELETE", "/api/storage/object"): (["60/60s@user"], None),
    ("GET", "/api/ops/slo"): ([], "60@public"),
    ("GET", "/api/ops/failures"): ([], "60@public"),
    ("GET", "/api/ops/digest"): ([], "60@public"),
    ("GET", "/api/ops/ga-gate"): ([], "60@public"),
    ("GET", "/api/ops/ga-gate/report"): ([], "60@public"),
    ("GET", "/api/integrations"): ([], "60@public"),
    ("GET", "/api/integrations/{slug}"): ([], "60@public"),
    ("GET", "/api/skills"): ([], "30@public"),
    ("GET", "/api/skills/{name}"): ([], "30@public"),
    ("GET", "/api/costs/today"): ([], "30@public"),
    ("GET", "/api/costs/history"): ([], "30@public"),
    ("GET", "/api/costs/by-tool"): ([], "30@public"),
    ("GET", "/api/costs/by-model"): ([], "30@public"),
    ("GET", "/api/audit/stats"): ([], "30@public"),
}


def _describe(endpoint: Any) -> tuple[list[str], str | None]:
    limits = []
    for limit in route_limits(endpoint) or ():
        if callable(limit.rate):
            limits.append(f"{limit.name}:{limit.strategy.name}@{limit.scope_name}")
        else:
            limits.append(f"{limit.rate.limit}/{limit.rate.period:g}s@{limit.scope_name}")
    rule = cache_rule(endpoint)
    return limits, None if rule is None else f"{rule.ttl:g}@{rule.scope}"


def test_switched_routes_carry_their_decorators() -> None:
    routes = _routes()
    for key, expected in EXPECTED.items():
        assert key in routes, key
        assert _describe(routes[key]) == expected, key


def test_no_other_route_is_decorated() -> None:
    """A new decorator on a route belongs in EXPECTED (and in the rework doc)."""
    decorated = {key for key, endpoint in _routes().items() if _describe(endpoint) != ([], None)}
    assert decorated == set(EXPECTED)


def test_the_hishel_cache_has_no_routes_left() -> None:
    """Everything shared-cached moved to @cache_response, so cache hits count against the limits."""
    from pincer.api.server import create_app

    assert cached_paths(create_app().routes) == []


# ── behaviour on the real app ─────────────────────────────────────────────────


def _limited_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str) -> TestClient:
    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    monkeypatch.setenv("PINCER_DASHBOARD_DIST", str(tmp_path / "no-dashboard"))
    monkeypatch.setenv("PINCER_RATE_API_DISABLED", "false")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings_relaxed.cache_clear()
    return TestClient(create_app())


class _Agent:
    def __init__(self) -> None:
        self.turns = 0

    async def handle_message(self, *args: Any, **kwargs: Any) -> Any:
        self.turns += 1
        return SimpleNamespace(
            text="hi",
            turn_text="hi",
            cost_usd=0.0,
            model="m",
            call_sids=[],
            budget_reached=False,
            budget_check_failed=False,
        )


def test_web_chat_shares_the_channel_message_limit(
    authed_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    client = _limited_client(monkeypatch, tmp_path, PINCER_RATE_MESSAGES_PER_MIN="2")
    agent = _Agent()
    client.app.state.agent = agent
    headers = authed_app.api_key_headers
    assert client.post("/api/chat/message", json={"text": "a"}, headers=headers).status_code == 200
    assert client.post("/api/chat/message", json={"text": "b"}, headers=headers).status_code == 200
    refused = client.post("/api/chat/stream", json={"text": "c"}, headers=headers)  # one budget for both
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("You're sending messages too fast! Please wait")
    assert agent.turns == 2


def test_costs_are_cached_and_stay_private_downstream(
    authed_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    client = _limited_client(monkeypatch, tmp_path)
    headers = authed_app.api_key_headers
    first = client.get("/api/costs/today", headers=headers)
    second = client.get("/api/costs/today", headers=headers)
    assert (first.status_code, second.status_code) == (200, 200)
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert second.headers["cache-control"] == "private, max-age=30"
    assert second.json() == first.json()


def test_storage_writes_share_one_budget() -> None:
    routes = _routes()
    names = set()
    for key in [("PUT", "/api/storage/text"), ("POST", "/api/storage/upload"), ("DELETE", "/api/storage/object")]:
        (limit,) = route_limits(routes[key]) or ()
        names.add(limit.bucket_name(key[1]))
    assert names == {"storage_write.60per60s"}  # one bucket, whichever write


# ── the demo call and the channel limiter on a shared backend ────────────────


@pytest.fixture
def shared_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = fakeredis.FakeServer()
    monkeypatch.setattr(backend, "get_redis", lambda url: fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(backend, "get_async_redis", lambda url: fakeredis.FakeAsyncRedis(server=server))
    return server


def _settings(url: str = "redis://localhost:6379/0") -> SimpleNamespace:
    return SimpleNamespace(cache_backend_url=url, cache_prefix="{environment}:{service}", environment="test")


async def test_demo_limits_hold_across_workers(shared_redis: Any) -> None:
    from fastapi import HTTPException

    from pincer.api import public_demo

    worker_a = public_demo._DemoLimits.from_settings(_settings())
    worker_b = public_demo._DemoLimits.from_settings(_settings())
    public_demo._limits = worker_a
    await public_demo._charge_limits("+4930111222", "10.0.0.1")
    public_demo._limits = worker_b
    with pytest.raises(HTTPException) as refused:
        await public_demo._charge_limits("+4930111222", "10.0.0.2")  # the other worker knows the number
    assert refused.value.status_code == 429
    assert "This number was just called" in refused.value.detail
    public_demo.reset_limits()


async def test_a_refused_demo_request_spends_nothing(shared_redis: Any) -> None:
    from fastapi import HTTPException

    from pincer.api import public_demo

    public_demo._limits = public_demo._DemoLimits.from_settings(_settings())
    await public_demo._charge_limits("+4930111222", "10.0.0.1")
    # The number is cooling down: this request must not use up the client's daily calls.
    for _ in range(5):
        with pytest.raises(HTTPException):
            await public_demo._charge_limits("+4930111222", "10.0.0.1")
    for i in range(public_demo.PER_CLIENT_DAILY - 1):
        await public_demo._charge_limits(f"+49301112{i:02d}", "10.0.0.1")
    public_demo.reset_limits()


async def test_demo_limits_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from pincer.api import public_demo
    from pincer.core.ratelimit import AsyncRateLimitStorage

    class Down(AsyncRateLimitStorage):
        async def hit(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

        async def reset(self, key: str) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr("pincer.core.ratelimit.storage.create_async_storage", lambda url: Down())
    public_demo._limits = public_demo._DemoLimits.from_settings(_settings())
    with pytest.raises(HTTPException) as refused:
        await public_demo._charge_limits("+4930111222", "10.0.0.1")
    assert refused.value.status_code == 503  # nobody is called while the counters can't be read
    assert "not available" in refused.value.detail
    public_demo.reset_limits()


async def test_channel_limiter_is_shared_across_processes(shared_redis: Any) -> None:
    from pincer.core.ratelimit import AsyncLimiterFactory
    from pincer.exceptions import RateLimitExceeded
    from pincer.security.rate_limiter import RateLimiter

    def process() -> RateLimiter:
        return RateLimiter(
            messages_per_minute=2,
            limiters=AsyncLimiterFactory.from_settings(_settings()),
        )

    main, worker = process(), process()
    await main.check_message("alice")
    assert (await worker.get_status("alice"))["messages_remaining"] == 1
    await worker.check_message("alice")
    with pytest.raises(RateLimitExceeded, match="too fast"):
        await main.check_message("alice")
    await main.cleanup_user("alice")
    await worker.check_message("alice")
