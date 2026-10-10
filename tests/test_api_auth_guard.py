"""API auth brute-force guard and CORS policy (Sprint 8, T8.2)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")


from pincer.api.auth_guard import MAX_LOCKOUT_SECONDS, AuthGuard, cors_origins, is_production


@pytest.fixture
def app(monkeypatch, request):
    monkeypatch.setenv("PINCER_AUTH_MAX_FAILURES", "3")
    monkeypatch.setenv("PINCER_AUTH_LOCKOUT_SECONDS", "60")
    # After the env is set: `authed_app` builds the app from it.
    return request.getfixturevalue("authed_app")


@pytest.fixture
def client(app):
    return app.client


@pytest.fixture
def good(app):
    return app.jwt_headers


def _expired_token() -> str:
    from pincer.security.credentials import encode_token

    return encode_token("j" * 48, sub="alice", typ="access", ver=0, ttl_seconds=-10)


# ── Brute-force guard (unit) ─────────────────────────────────────────


def test_guard_allows_attempts_under_the_budget():
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    for _ in range(3):
        assert guard.record_failure("1.2.3.4") == 0
    assert guard.retry_after("1.2.3.4") == 0


def test_guard_locks_out_after_the_budget():
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    for _ in range(3):
        guard.record_failure("1.2.3.4")
    assert guard.record_failure("1.2.3.4") == 60
    assert 0 < guard.retry_after("1.2.3.4") <= 61


def test_lockout_backs_off_exponentially():
    guard = AuthGuard(max_failures=1, lockout_seconds=10)
    guard.record_failure("1.2.3.4")
    assert [guard.record_failure("1.2.3.4") for _ in range(3)] == [10, 20, 40]


def test_lockout_is_capped():
    guard = AuthGuard(max_failures=1, lockout_seconds=60)
    for _ in range(40):
        last = guard.record_failure("1.2.3.4")
    assert last == MAX_LOCKOUT_SECONDS


def test_lockout_is_per_ip():
    guard = AuthGuard(max_failures=1, lockout_seconds=60)
    guard.record_failure("1.1.1.1")
    guard.record_failure("1.1.1.1")
    assert guard.retry_after("1.1.1.1") > 0
    assert guard.retry_after("2.2.2.2") == 0


def test_success_clears_the_failure_history():
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    guard.record_failure("1.2.3.4")
    guard.record_failure("1.2.3.4")
    guard.record_success("1.2.3.4")
    assert guard.record_failure("1.2.3.4") == 0  # budget reset


def test_a_non_escalating_guard_locks_for_one_flat_window_then_resets(monkeypatch):
    """The per-account guard: its key can be aimed at someone else, so a
    lockout never grows and is followed by a full budget."""
    import time as time_module

    clock = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
    guard = AuthGuard(max_failures=2, lockout_seconds=60, escalate=False)

    assert [guard.record_failure("alice") for _ in range(3)] == [0, 0, 60]
    assert guard.retry_after("alice") > 0
    clock[0] += 61
    assert guard.retry_after("alice") == 0
    # A fresh budget, and the same flat window when it is spent again.
    assert [guard.record_failure("alice") for _ in range(3)] == [0, 0, 60]

    escalating = AuthGuard(max_failures=2, lockout_seconds=60)
    assert [escalating.record_failure("1.2.3.4") for _ in range(3)] == [0, 0, 60]
    clock[0] += 61
    assert escalating.record_failure("1.2.3.4") == 120


# ── Brute-force guard (through the API) ──────────────────────────────


def test_valid_token_is_accepted(client, good):
    assert client.get("/api/status", headers=good).status_code == 200


def test_invalid_token_is_401(client):
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_repeated_failures_lock_the_ip_out(client, good):
    # PINCER_AUTH_MAX_FAILURES=3 → three attempts are free, the fourth locks.
    for _ in range(4):
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

    locked = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0

    # A locked-out IP does not get a second chance even with the right token —
    # otherwise the lockout is just a slow oracle.
    assert client.get("/api/status", headers=good).status_code == 429


def test_lockout_is_scoped_to_the_forwarded_ip(client, good):
    for _ in range(5):
        client.get("/api/status", headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "10.0.0.1"})
    assert (
        client.get("/api/status", headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "10.0.0.1"}).status_code
        == 429
    )
    # A different client behind the same proxy is unaffected.
    assert client.get("/api/status", headers={**good, "X-Forwarded-For": "10.0.0.2"}).status_code == 200


def test_an_api_key_is_accepted_and_a_wrong_one_counts(client, app):
    assert client.get("/api/status", headers=app.api_key_headers).status_code == 200
    for _ in range(4):
        assert client.get("/api/status", headers={"Authorization": "Bearer pnc_wrong"}).status_code == 401
    assert client.get("/api/status", headers=app.api_key_headers).status_code == 429


def test_expired_token_is_not_a_lockout_failure(client, good):
    """When a session token runs out, every request the dashboard has in
    flight fails at once. Those are not guesses — the token is genuinely
    signed — so they must not lock the dashboard's own user out."""
    expired = {"Authorization": f"Bearer {_expired_token()}"}
    for _ in range(10):
        resp = client.get("/api/status", headers=expired)
        assert resp.status_code == 401
        assert resp.json()["error"] == "token_expired"
    assert client.get("/api/status", headers=good).status_code == 200


def test_token_from_before_a_password_change_is_not_a_lockout_failure(client, app, good):
    app.run(lambda: app.auth.set_password("alice", "a brand new password"))
    for _ in range(10):
        resp = client.get("/api/status", headers=good)
        assert resp.status_code == 401
        assert resp.json()["error"] == "token_expired"

    fresh = client.post("/api/auth/login", json={"identifier": "alice", "password": "a brand new password"})
    assert fresh.status_code == 200
    headers = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
    assert client.get("/api/status", headers=headers).status_code == 200


def test_expired_token_is_not_audit_logged_as_a_failure(client, monkeypatch):
    logged: list[object] = []

    async def _fake_audit(ip, path, reason, locked_for=0):
        logged.append(reason)

    monkeypatch.setattr("pincer.api.server.audit_auth_failure", _fake_audit)
    client.get("/api/status", headers={"Authorization": f"Bearer {_expired_token()}"})
    assert logged == []


def test_login_shares_the_ip_lockout(client, app):
    for _ in range(4):
        client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    resp = client.post("/api/auth/login", json={"identifier": "alice", "password": app.password})
    assert resp.status_code == 429


def test_health_endpoint_is_never_rate_limited(client):
    for _ in range(8):
        client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert client.get("/api/health").status_code == 200


def test_failed_auth_is_audit_logged(client, monkeypatch):
    logged: list[object] = []

    async def _fake_audit(ip, path, reason, locked_for=0):
        logged.append((ip, path, reason))

    monkeypatch.setattr("pincer.api.server.audit_auth_failure", _fake_audit)
    client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert logged and logged[0][1] == "/api/status" and logged[0][2] == "invalid_token"
    assert logged[0][0]  # an IP was recorded


# ── CORS origin policy ───────────────────────────────────────────────


def _cfg(**overrides):
    cfg = MagicMock()
    cfg.environment = "development"
    cfg.dashboard_url = "https://dashboard.example.com"
    cfg.web_chat_url = ""
    cfg.cors_extra_origins = ""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_development_allows_localhost():
    origins = cors_origins(_cfg())
    assert "http://localhost:3000" in origins
    assert "https://dashboard.example.com" in origins


def test_production_drops_localhost():
    origins = cors_origins(_cfg(environment="production"))
    assert origins == ["https://dashboard.example.com"]
    assert not [o for o in origins if "localhost" in o or "127.0.0.1" in o]


def test_production_keeps_configured_extra_origins():
    origins = cors_origins(
        _cfg(
            environment="production",
            web_chat_url="https://chat.example.com",
            cors_extra_origins="https://app.example.com, https://admin.example.com",
        )
    )
    assert origins == [
        "https://dashboard.example.com",
        "https://chat.example.com",
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_empty_origins_are_never_allowed():
    assert "" not in cors_origins(_cfg(dashboard_url="", web_chat_url=""))


def test_origins_are_deduplicated():
    origins = cors_origins(_cfg(environment="production", web_chat_url="https://dashboard.example.com"))
    assert origins == ["https://dashboard.example.com"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("production", True), ("Production", True), ("development", False), ("staging", False), ("", False)],
)
def test_is_production(value, expected):
    assert is_production(_cfg(environment=value)) is expected
