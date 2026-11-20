"""HTTP API rate limits (issue #404): the IP ceiling before auth, user/anon limits after it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import AsyncLimiter, AsyncMemoryStorage, Rate, RateLimitMiddleware


def _client(monkeypatch: pytest.MonkeyPatch, *, ip: int = 1000, user: int = 1000, anon: int = 1000) -> TestClient:
    """A fresh app (and so fresh in-memory buckets) with rate limiting on."""
    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    monkeypatch.setenv("PINCER_RATE_API_DISABLED", "false")
    monkeypatch.setenv("PINCER_RATE_API_IP_PER_MIN", str(ip))
    monkeypatch.setenv("PINCER_RATE_API_USER_PER_MIN", str(user))
    monkeypatch.setenv("PINCER_RATE_API_ANON_PER_MIN", str(anon))
    get_settings_relaxed.cache_clear()
    return TestClient(create_app())


def _bob_api_key(authed_app: Any) -> str:
    async def seed() -> str:
        await authed_app.identities.create_profile("bob", email="bob@example.com", display_name="Bob")
        key, _ = await authed_app.auth.generate_api_key("bob")
        return key

    return authed_app.run(seed)


def _statuses(client: TestClient, n: int, path: str = "/api/identity/me", **kwargs: Any) -> list[int]:
    return [client.get(path, **kwargs).status_code for _ in range(n)]


def test_disabled_by_default_in_tests(authed_app: Any) -> None:
    assert authed_app.client.app.state.rate_limits is None


def test_user_limit_applies_per_identity_behind_one_ip(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bob = {"Authorization": f"Bearer {_bob_api_key(authed_app)}"}
    client = _client(monkeypatch, user=2)
    alice = authed_app.api_key_headers

    assert _statuses(client, 3, headers=alice) == [200, 200, 429]
    # Same client IP, another user: a budget of its own.
    assert _statuses(client, 2, headers=bob) == [200, 200]

    denied = client.get("/api/identity/me", headers=alice)
    assert denied.status_code == 429
    assert denied.json() == {"detail": "Rate limit exceeded"}
    assert int(denied.headers["retry-after"]) >= 1
    assert denied.headers["x-ratelimit-limit"] == "2"
    assert denied.headers["x-ratelimit-remaining"] == "0"


def test_allowed_responses_carry_the_user_limit_headers(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, user=5)
    response = client.get("/api/identity/me", headers=authed_app.jwt_headers)
    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit"] == "5"
    assert response.headers["x-ratelimit-remaining"] == "4"
    assert int(response.headers["x-ratelimit-reset"]) <= 60


def test_ip_ceiling_counts_every_user_on_that_ip(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bob = {"Authorization": f"Bearer {_bob_api_key(authed_app)}"}
    client = _client(monkeypatch, ip=3)
    assert _statuses(client, 2, headers=authed_app.api_key_headers) == [200, 200]
    assert _statuses(client, 2, headers=bob) == [200, 429]


def test_ip_ceiling_refuses_before_auth(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, ip=2)
    bad = {"Authorization": "Bearer not-a-token"}
    assert _statuses(client, 3, headers=bad) == [401, 401, 429]


def test_ip_ceiling_is_keyed_by_forwarded_ip(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, ip=1)
    headers = authed_app.api_key_headers
    assert client.get("/api/identity/me", headers={**headers, "X-Forwarded-For": "203.0.113.1"}).status_code == 200
    assert client.get("/api/identity/me", headers={**headers, "X-Forwarded-For": "203.0.113.1"}).status_code == 429
    assert client.get("/api/identity/me", headers={**headers, "X-Forwarded-For": "2001:db8::1"}).status_code == 200


def test_rejected_token_is_not_charged_to_the_user(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, user=2)
    for _ in range(3):
        assert client.get("/api/identity/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert _statuses(client, 2, headers=authed_app.api_key_headers) == [200, 200]


def test_requests_without_identity_use_the_anon_limit(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, anon=2, user=100)
    assert _statuses(client, 3, path="/api/docs") == [200, 200, 429]
    # The user bucket is a different one.
    assert client.get("/api/identity/me", headers=authed_app.api_key_headers).status_code == 200


@pytest.mark.parametrize("path", ["/api/health", "/", "/api/apps/twilio/voice/status"])
def test_exempt_paths_are_never_limited(authed_app: Any, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    client = _client(monkeypatch, ip=1, anon=1)
    statuses = _statuses(client, 3, path=path)
    assert 429 not in statuses


def test_bucket_keys_use_the_cache_prefix(authed_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_CACHE_PREFIX", "{environment}:{service}:tenant")
    monkeypatch.setenv("PINCER_ENVIRONMENT", "staging")
    client = _client(monkeypatch)
    limits = client.app.state.rate_limits
    assert limits.user.key("alice") == "staging:pincer:tenant:kv:ratelimit:user:alice"
    assert limits.ip.key("10.0.0.1") == "staging:pincer:tenant:kv:ratelimit:ip:10.0.0.1"


# ── The middleware itself, on a bare app ─────────────────────────────────────


async def _stream(request: Any) -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        for i in range(3):
            yield f"data: {i}\n\n".encode()

    return StreamingResponse(chunks(), media_type="text/event-stream")


def _bare_app(limit: int) -> Starlette:
    limiter = AsyncLimiter(AsyncMemoryStorage(), Rate(limit, 60), KeySpace("t:pincer"), "ip")
    app = Starlette(routes=[Route("/stream", _stream)])
    app.add_middleware(RateLimitMiddleware, identify=lambda scope: (limiter, "client"))
    return app


def test_streamed_responses_pass_through_with_headers() -> None:
    with TestClient(_bare_app(5)) as client, client.stream("GET", "/stream") as response:
        body = b"".join(response.iter_bytes())
        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining"] == "4"
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_middleware_rejects_once_the_bucket_is_empty() -> None:
    client = TestClient(_bare_app(1))
    assert client.get("/stream").status_code == 200
    assert client.get("/stream").status_code == 429
