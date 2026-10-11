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


def test_reserve_admits_no_more_attempts_at_once_than_the_budget_has_left():
    """Attempts sent together are all checked before any of them has failed,
    so the ones still being verified count against the budget."""
    guard = AuthGuard(max_failures=3, lockout_seconds=60)

    # 3 free failures and the one that triggers the lockout: 4 may run at once.
    assert [guard.reserve("1.2.3.4") for _ in range(6)] == [0, 0, 0, 0, 1, 1]
    assert guard.reserve("9.9.9.9") == 0  # per key

    guard.release("1.2.3.4")
    guard.record_failure("1.2.3.4")
    # One failure recorded, three still in flight: nothing is free yet.
    assert guard.reserve("1.2.3.4") == 1
    for _ in range(3):
        guard.release("1.2.3.4")
        guard.record_failure("1.2.3.4")
    assert guard.reserve("1.2.3.4") > 1  # locked out now, for the real window


def test_reserve_keeps_sequential_attempts_as_they_were():
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    for _ in range(4):
        assert guard.reserve("1.2.3.4") == 0
        guard.release("1.2.3.4")
        guard.record_failure("1.2.3.4")
    assert 0 < guard.reserve("1.2.3.4") <= 61


def test_release_and_success_tolerate_what_is_not_there():
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    guard.release("never-seen")
    guard.record_success("never-seen")

    assert guard.reserve("1.2.3.4") == 0
    assert guard.reserve("1.2.3.4") == 0
    guard.release("1.2.3.4")
    # A success while another attempt is in flight keeps that reservation.
    guard.record_success("1.2.3.4")
    assert guard._ips["1.2.3.4"].in_flight == 1
    guard.release("1.2.3.4")
    guard.release("1.2.3.4")  # one too many is not a negative budget
    assert guard._ips["1.2.3.4"].in_flight == 0
    guard.record_success("1.2.3.4")
    assert "1.2.3.4" not in guard._ips


def test_an_entry_with_an_attempt_in_flight_is_never_pruned(monkeypatch):
    import time as time_module

    clock = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
    guard = AuthGuard(max_failures=3, lockout_seconds=60)
    assert guard.reserve("slow") == 0
    assert guard.reserve("done") == 0
    guard.release("done")

    clock[0] += 60 + MAX_LOCKOUT_SECONDS + 1
    guard.retry_after("anyone")

    assert "slow" in guard._ips
    assert "done" not in guard._ips


def test_reserve_after_a_flat_window_has_a_full_budget_and_an_escalating_one_does_not(monkeypatch):
    import time as time_module

    clock = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
    flat = AuthGuard(max_failures=2, lockout_seconds=60, escalate=False)
    escalating = AuthGuard(max_failures=2, lockout_seconds=60)
    for guard in (flat, escalating):
        assert [guard.record_failure("alice") for _ in range(3)] == [0, 0, 60]
        assert guard.reserve("alice") > 1

    clock[0] += 61
    # Flat: the window is over, the account has its whole budget back.
    assert [flat.reserve("alice") for _ in range(4)] == [0, 0, 0, 1]
    # Escalating: the history stands, so one attempt at a time.
    assert [escalating.reserve("alice") for _ in range(2)] == [0, 1]


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
