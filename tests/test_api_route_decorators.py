"""Per-route `@rate_limit` / `@no_rate_limit` and `@cache_response`, alone and combined (issue #404).

A bare FastAPI app is wired the way `create_app` wires pincer's — the
`enforce_rate_limits` app dependency, `app.state.rate_limits` and
`app.state.response_cache` — with a stand-in auth middleware that takes the
caller from `X-User`. Every test runs on the memory backend and on fakeredis.
The last test goes through the real `create_app`.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pincer.api.rate_limits import APIRateLimits, enforce_rate_limits
from pincer.core.cache import backend
from pincer.core.cache.decorators import ResponseCache, cache_response, response_cache
from pincer.core.cache.middleware import cache_control, cached_paths
from pincer.core.ratelimit import Rate, no_rate_limit, rate_limit
from pincer.core.ratelimit.decorators import route_limits

fakeredis = pytest.importorskip("fakeredis")


def _settings(url: str, **limits: int) -> SimpleNamespace:
    return SimpleNamespace(
        cache_backend_url=url,
        cache_prefix="{environment}:{service}",
        environment="test",
        cache_default_ttl=300,
        rate_api_ip_per_min=limits.get("ip", 10_000),
        rate_api_user_per_min=limits.get("user", 100),
        rate_api_anon_per_min=limits.get("anon", 100),
    )


DYNAMIC_RATE = {"per_minute": 1}


class Out(BaseModel):
    name: str


def _build(settings: SimpleNamespace) -> tuple[FastAPI, dict[str, int]]:
    calls: dict[str, int] = {}

    def count(name: str) -> int:
        calls[name] = calls.get(name, 0) + 1
        return calls[name]

    app = FastAPI(dependencies=[Depends(enforce_rate_limits)])
    app.state.rate_limits = APIRateLimits.from_settings(settings)
    app.state.response_cache = ResponseCache.from_settings(settings)

    @app.middleware("http")
    async def fake_auth(request: Request, call_next: Any) -> Any:
        user = request.headers.get("x-user")
        request.state.identity = SimpleNamespace(pincer_user_id=user) if user else None
        return await call_next(request)

    # ── rate limits ──
    @app.get("/api/default")
    async def default() -> dict[str, int]:
        return {"n": count("default")}

    @app.get("/api/limited")
    @rate_limit("2/minute")
    async def limited() -> dict[str, int]:
        return {"n": count("limited")}

    @app.get("/api/stacked")
    @rate_limit("5/minute")
    @rate_limit("2/minute", strategy="token_bucket")
    async def stacked() -> dict[str, int]:
        return {"n": count("stacked")}

    @app.get("/api/free")
    @no_rate_limit
    def free() -> dict[str, int]:
        return {"n": count("free")}

    @app.get("/api/by-ip")
    @rate_limit("2/minute", scope="ip")
    async def by_ip() -> dict[str, int]:
        return {"n": count("by_ip")}

    def tenant(request: Request) -> str | None:
        return request.headers.get("x-tenant")

    @app.get("/api/by-tenant")
    @rate_limit("1/minute", scope=tenant)
    async def by_tenant() -> dict[str, int]:
        return {"n": count("by_tenant")}

    @app.get("/api/minute-and-day")
    @rate_limit("1/minute")
    @rate_limit("3/day", name="shared_day")
    async def minute_and_day() -> dict[str, int]:
        return {"n": count("minute_and_day")}

    @app.get("/api/day-only")
    @rate_limit("3/day", name="shared_day")
    async def day_only() -> dict[str, int]:
        return {"n": count("day_only")}

    @app.get("/api/detail-text")
    @rate_limit("1/minute", detail="Slow down, please.")
    async def detail_text() -> dict[str, int]:
        return {"n": count("detail_text")}

    @app.get("/api/detail-fn")
    @rate_limit("1/minute", detail=lambda result: f"Wait {result.retry_after:.0f}s.")
    async def detail_fn() -> dict[str, int]:
        return {"n": count("detail_fn")}

    @app.get("/api/everyone")
    @rate_limit("2/minute", scope="global")
    async def everyone() -> dict[str, int]:
        return {"n": count("everyone")}

    @app.get("/api/dynamic")
    @rate_limit(lambda: f"{DYNAMIC_RATE['per_minute']}/minute")
    async def dynamic() -> dict[str, int]:
        return {"n": count("dynamic")}

    # ── response cache ──
    @app.get("/api/cached")
    @cache_response(ttl=60)
    async def cached(q: str = "") -> dict[str, Any]:
        return {"n": count("cached"), "q": q}

    @app.get("/api/cached-sync")
    @cache_response(ttl=60)
    def cached_sync() -> dict[str, int]:
        return {"n": count("cached_sync")}

    @app.get("/api/public")
    @cache_response(ttl=60, scope="public")
    async def public() -> dict[str, int]:
        return {"n": count("public")}

    @app.get("/api/model", response_model=Out)
    @cache_response(ttl=60)
    async def model() -> dict[str, Any]:
        return {"name": f"n{count('model')}", "secret": "never sent"}

    @app.get("/api/raw")
    @cache_response(ttl=60)
    async def raw() -> PlainTextResponse:
        return PlainTextResponse(f"raw {count('raw')}")

    @app.get("/api/both", dependencies=[cache_control(max_age=60, public=True)])
    @cache_response(ttl=60)
    async def both() -> dict[str, int]:
        return {"n": count("both")}

    # ── both decorators, both orders, both modes ──
    @app.get("/api/combo-cache-outer")
    @cache_response(ttl=60)
    @rate_limit("3/minute")
    async def combo_cache_outer() -> dict[str, int]:
        return {"n": count("combo_cache_outer")}

    @app.get("/api/combo-limit-outer")
    @rate_limit("3/minute")
    @cache_response(ttl=60)
    async def combo_limit_outer() -> dict[str, int]:
        return {"n": count("combo_limit_outer")}

    @app.get("/api/combo-sync-cache-outer")
    @cache_response(ttl=60)
    @rate_limit("3/minute")
    def combo_sync_cache_outer() -> dict[str, int]:
        return {"n": count("combo_sync_cache_outer")}

    @app.get("/api/combo-sync-limit-outer")
    @rate_limit("3/minute")
    @cache_response(ttl=60)
    def combo_sync_limit_outer() -> dict[str, int]:
        return {"n": count("combo_sync_limit_outer")}

    return app, calls


@pytest.fixture(params=["memory", "redis"])
def backend_url(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "memory":
        return "memory://"
    server = fakeredis.FakeServer()
    monkeypatch.setattr(backend, "get_redis", lambda url: fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(backend, "get_async_redis", lambda url: fakeredis.FakeAsyncRedis(server=server))
    return "redis://localhost:6379/0"


@pytest.fixture
def make(backend_url: str) -> Iterator[Any]:
    clients: list[TestClient] = []

    def factory(**limits: int) -> tuple[TestClient, dict[str, int]]:
        app, calls = _build(_settings(backend_url, **limits))
        client = TestClient(app)
        client.__enter__()  # one event loop for the client's lifetime (async Redis clients bind to it)
        clients.append(client)
        return client, calls

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


ALICE = {"X-User": "alice"}
BOB = {"X-User": "bob"}


def _endpoint(client: TestClient, path: str) -> Any:
    """The endpoint function serving *path*, e.g. to invalidate its cache."""
    return next(route.endpoint for route in client.app.routes if getattr(route, "path", "") == path)


def _codes(client: TestClient, path: str, n: int, headers: dict[str, str] | None = None) -> list[int]:
    return [client.get(path, headers=headers or ALICE).status_code for _ in range(n)]


# ── @rate_limit / @no_rate_limit ─────────────────────────────────────────────


def test_route_limit_replaces_the_default(make: Any) -> None:
    client, _ = make(user=100)
    assert _codes(client, "/api/limited", 3) == [200, 200, 429]
    assert _codes(client, "/api/default", 3) == [200, 200, 200]  # the default user limit is untouched


def test_route_buckets_are_apart_from_the_default(make: Any) -> None:
    client, _ = make(user=2)
    assert _codes(client, "/api/default", 3) == [200, 200, 429]  # the default budget is spent...
    assert _codes(client, "/api/limited", 2) == [200, 200]  # ...a decorated route has its own
    response = client.get("/api/limited", headers=BOB)
    assert response.headers["x-ratelimit-limit"] == "2"  # its own limit, not the default


def test_stacked_limits_all_apply(make: Any) -> None:
    client, _ = make()
    first = client.get("/api/stacked", headers=ALICE)
    assert first.headers["x-ratelimit-limit"] == "2"  # the tighter one is reported
    assert _codes(client, "/api/stacked", 2) == [200, 429]


def test_a_refused_stacked_limit_spends_nothing_on_the_others(make: Any) -> None:
    """Every stacked limit is checked before any is charged (`acharge_all`)."""
    client, _ = make()
    # 1 allowed, then 3 refused by the minute limit: the day budget (shared by name) spent only once.
    assert _codes(client, "/api/minute-and-day", 4) == [200, 429, 429, 429]
    assert _codes(client, "/api/day-only", 3) == [200, 200, 429]


def test_no_rate_limit_skips_the_default(make: Any) -> None:
    client, calls = make(user=1)
    assert _codes(client, "/api/free", 5) == [200] * 5
    assert calls["free"] == 5


def test_refusal_body_and_headers(make: Any) -> None:
    client, _ = make()
    _codes(client, "/api/limited", 2)
    denied = client.get("/api/limited", headers=ALICE)
    assert denied.status_code == 429
    assert denied.json() == {"detail": "Rate limit exceeded"}
    assert int(denied.headers["retry-after"]) >= 1
    assert denied.headers["x-ratelimit-limit"] == "2"
    assert denied.headers["x-ratelimit-remaining"] == "0"


def test_scope_ip_counts_users_on_one_ip_together(make: Any) -> None:
    client, _ = make()
    assert client.get("/api/by-ip", headers=ALICE).status_code == 200
    assert client.get("/api/by-ip", headers=BOB).status_code == 200
    assert client.get("/api/by-ip", headers={"X-User": "carol"}).status_code == 429


def test_scope_function_picks_the_bucket(make: Any) -> None:
    client, _ = make()
    assert _codes(client, "/api/by-tenant", 2, {"X-Tenant": "acme"}) == [200, 429]
    assert client.get("/api/by-tenant", headers={"X-Tenant": "other"}).status_code == 200
    assert _codes(client, "/api/by-tenant", 3, {}) == [200, 200, 200]  # None: not limited


def test_user_scope_without_identity_counts_the_ip(make: Any) -> None:
    client, _ = make()
    assert [client.get("/api/limited").status_code for _ in range(3)] == [200, 200, 429]


def test_decorator_validation() -> None:
    with pytest.raises(ValueError, match="Invalid rate"):
        rate_limit("lots")
    with pytest.raises(ValueError, match="Unknown rate limit scope"):
        rate_limit("1/minute", scope="tenant")
    with pytest.raises(ValueError, match="Unknown rate limit strategy"):
        rate_limit("1/minute", strategy="leaky")

    def endpoint() -> None: ...

    with pytest.raises(TypeError, match="no_rate_limit"):
        rate_limit("1/minute")(no_rate_limit(endpoint))
    assert route_limits(endpoint) is None
    assert route_limits(lambda: None) == ()
    assert Rate.parse("2/minute") == Rate(2, 60)


def test_detail_replaces_the_refusal_message(make: Any) -> None:
    client, _ = make()
    client.get("/api/detail-text", headers=ALICE)
    assert client.get("/api/detail-text", headers=ALICE).json() == {"detail": "Slow down, please."}
    client.get("/api/detail-fn", headers=ALICE)
    refused = client.get("/api/detail-fn", headers=ALICE)
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("Wait ") and refused.json()["detail"].endswith("s.")


def test_global_scope_is_one_bucket_for_everyone(make: Any) -> None:
    client, _ = make()
    assert client.get("/api/everyone", headers=ALICE).status_code == 200
    assert client.get("/api/everyone", headers=BOB).status_code == 200
    assert client.get("/api/everyone").status_code == 429  # nobody else, whoever asks


def test_a_rate_function_is_read_per_request(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make()
    monkeypatch.setitem(DYNAMIC_RATE, "per_minute", 1)
    assert _codes(client, "/api/dynamic", 2) == [200, 429]
    monkeypatch.setitem(DYNAMIC_RATE, "per_minute", 3)  # a new rate: a new bucket
    assert _codes(client, "/api/dynamic", 4) == [200, 200, 200, 429]
    assert client.get("/api/dynamic", headers=BOB).headers["x-ratelimit-limit"] == "3"


# ── @cache_response ──────────────────────────────────────────────────────────


def test_hit_skips_the_endpoint(make: Any) -> None:
    client, calls = make()
    first = client.get("/api/cached", headers=ALICE)
    second = client.get("/api/cached", headers=ALICE)
    assert first.json() == second.json() == {"n": 1, "q": ""}
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert second.headers["cache-control"] == "private, max-age=60"
    assert calls["cached"] == 1
    assert client.get("/api/cached?q=x", headers=ALICE).json() == {"n": 2, "q": "x"}  # the query varies


def test_users_have_their_own_entries(make: Any) -> None:
    client, calls = make()
    client.get("/api/cached", headers=ALICE)
    assert client.get("/api/cached", headers=BOB).json()["n"] == 2
    assert calls["cached"] == 2


def test_no_identity_is_not_cached(make: Any) -> None:
    client, calls = make()
    for _ in range(2):
        assert client.get("/api/cached").headers["x-cache"] == "BYPASS"
    assert calls["cached"] == 2


def test_public_scope_is_shared(make: Any) -> None:
    client, calls = make()
    client.get("/api/public", headers=ALICE)
    hit = client.get("/api/public", headers=BOB)
    assert hit.headers["x-cache"] == "HIT"
    assert hit.headers["cache-control"] == "public, max-age=60"
    assert calls["public"] == 1


def test_public_scope_stays_private_downstream_for_an_authenticated_request(make: Any) -> None:
    """Shared inside pincer, but a proxy must never hand an authenticated answer to someone else."""
    client, _ = make()
    response = client.get("/api/public", headers={**ALICE, "Authorization": "Bearer pnc_x"})
    assert response.headers["cache-control"] == "private, max-age=60"


def test_response_model_applies_on_a_hit(make: Any) -> None:
    client, _ = make()
    assert client.get("/api/model", headers=ALICE).json() == {"name": "n1"}
    hit = client.get("/api/model", headers=ALICE)
    assert hit.headers["x-cache"] == "HIT"
    assert hit.json() == {"name": "n1"}  # "secret" filtered out, as on the miss


def test_response_objects_are_not_cached(make: Any) -> None:
    client, calls = make()
    assert client.get("/api/raw", headers=ALICE).text == "raw 1"
    assert client.get("/api/raw", headers=ALICE).text == "raw 2"
    assert calls["raw"] == 2


def test_sync_endpoint_uses_the_sync_store(make: Any) -> None:
    client, calls = make()
    client.get("/api/cached-sync", headers=ALICE)
    assert client.get("/api/cached-sync", headers=ALICE).headers["x-cache"] == "HIT"
    assert calls["cached_sync"] == 1
    cache = response_cache(client.app)
    key = cache.prefix(_endpoint(client, "/api/cached-sync"), "alice")
    assert cache.store.delete_prefix(key) == 1  # the sync store holds it (and shares it with the async one)


def test_decorator_overrides_cache_control(make: Any) -> None:
    client, calls = make()
    assert all("both" not in p.pattern for p in cached_paths(client.app.routes))  # hishel skips it
    client.get("/api/both", headers=ALICE)
    assert client.get("/api/both", headers=BOB).json()["n"] == 2  # per user, not shared by URL
    assert calls["both"] == 2


async def test_invalidation_by_route_and_user(make: Any) -> None:
    client, calls = make()
    for who in (ALICE, BOB):
        client.get("/api/cached", headers=who)
    cache = response_cache(client.app)
    cached = _endpoint(client, "/api/cached")
    assert cache.invalidate(cached, user="alice") == 1
    assert client.get("/api/cached", headers=ALICE).headers["x-cache"] == "MISS"
    assert client.get("/api/cached", headers=BOB).headers["x-cache"] == "HIT"
    assert cache.invalidate(cached) == 2
    assert client.get("/api/cached", headers=BOB).headers["x-cache"] == "MISS"
    assert calls["cached"] == 4


def test_same_function_names_never_share_entries() -> None:
    from pincer.core.cache.decorators import cache_name, cache_rule

    def make_a() -> Any:
        async def listing() -> dict[str, str]:
            return {}

        return listing

    def make_b() -> Any:
        async def listing() -> dict[str, str]:
            return {}

        return listing

    a = cache_response(scope="public")(make_a())
    b = cache_response(scope="public")(make_b())
    assert a.__name__ == b.__name__ == "listing"
    assert cache_rule(a).name != cache_rule(b).name
    assert cache_rule(a).name.startswith(f"{__name__}.")
    assert cache_name(cache_response(name="custom")(make_a())) == "custom"
    with pytest.raises(ValueError, match="no @cache_response"):
        cache_name(make_a())


def test_cache_decorator_validation() -> None:
    with pytest.raises(ValueError, match="Unknown cache scope"):
        cache_response(scope="ip")
    with pytest.raises(ValueError, match="ttl must be positive"):
        cache_response(ttl=0)


# ── @rate_limit + @cache_response on one route ───────────────────────────────

COMBOS = ["combo-cache-outer", "combo-limit-outer", "combo-sync-cache-outer", "combo-sync-limit-outer"]


@pytest.mark.parametrize("path", COMBOS)
def test_cache_hits_still_count_against_the_limit(make: Any, path: str) -> None:
    client, calls = make(user=100)
    responses = [client.get(f"/api/{path}", headers=ALICE) for _ in range(4)]
    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert [r.headers.get("x-cache") for r in responses[:3]] == ["MISS", "HIT", "HIT"]
    assert "x-cache" not in responses[3].headers  # a refusal is never cached or served from cache
    assert sum(calls.values()) == 1  # the endpoint body ran once


@pytest.mark.parametrize("path", COMBOS)
def test_users_are_isolated_in_both(make: Any, path: str) -> None:
    client, _ = make()
    _codes(client, f"/api/{path}", 3)
    assert client.get(f"/api/{path}", headers=ALICE).status_code == 429
    bob = [client.get(f"/api/{path}", headers=BOB) for _ in range(3)]
    assert [r.status_code for r in bob] == [200, 200, 200]  # a budget of its own
    assert bob[0].headers["x-cache"] == "MISS"  # and an entry of its own


@pytest.mark.parametrize("path", COMBOS)
def test_invalidation_does_not_touch_the_limit(make: Any, path: str) -> None:
    client, _ = make()
    _codes(client, f"/api/{path}", 2)
    assert response_cache(client.app).invalidate(_endpoint(client, f"/api/{path}"), user="alice") == 1
    third = client.get(f"/api/{path}", headers=ALICE)
    assert (third.status_code, third.headers["x-cache"]) == (200, "MISS")
    assert client.get(f"/api/{path}", headers=ALICE).status_code == 429  # still the 4th request


@pytest.mark.parametrize("path", COMBOS)
def test_hit_reports_the_route_limit(make: Any, path: str) -> None:
    client, _ = make(user=100)
    client.get(f"/api/{path}", headers=ALICE)
    hit = client.get(f"/api/{path}", headers=ALICE)
    assert hit.headers["x-cache"] == "HIT"
    assert hit.headers["x-ratelimit-limit"] == "3"  # the route's limit, not the default 100
    assert hit.headers["x-ratelimit-remaining"] == "1"


# ── through the real create_app ──────────────────────────────────────────────


def test_decorators_on_a_pincer_route(authed_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from fastapi import APIRouter

    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    # No dashboard build: its catch-all route would answer /api/demo/* (added after it) with 404.
    monkeypatch.setenv("PINCER_DASHBOARD_DIST", str(tmp_path / "no-dashboard"))
    monkeypatch.setenv("PINCER_RATE_API_DISABLED", "false")
    monkeypatch.setenv("PINCER_RATE_API_IP_PER_MIN", "5")
    get_settings_relaxed.cache_clear()
    app = create_app()
    router = APIRouter(prefix="/api/demo")
    calls: list[int] = []

    @router.get("/thing")
    @rate_limit("3/minute")
    @cache_response(ttl=60)
    async def thing() -> dict[str, int]:
        calls.append(1)
        return {"n": len(calls)}

    app.include_router(router)
    with TestClient(app) as client:
        headers = authed_app.api_key_headers
        codes = [client.get("/api/demo/thing", headers=headers) for _ in range(4)]
        assert [r.status_code for r in codes] == [200, 200, 200, 429]
        assert codes[1].headers["x-cache"] == "HIT"
        assert len(calls) == 1
        # The pre-auth IP ceiling (5) still applies to a decorated route.
        assert client.get("/api/demo/thing", headers=headers).status_code == 429
        assert client.get("/api/identity/me", headers=headers).status_code == 429
    get_settings_relaxed.cache_clear()
