"""Signing in: `POST /api/auth/login` and `/refresh` (issue #228)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from argon2 import PasswordHasher

from pincer.db.session import session_scope
from pincer.repositories.identity import IdentityCredentialRepository
from pincer.security import credentials
from pincer.services.auth import (
    ApiKeyExistsError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenStaleError,
    UnknownIdentityError,
)

if TYPE_CHECKING:
    from conftest import AuthedApp

LOGIN = "/api/auth/login"
REFRESH = "/api/auth/refresh"


def _login(app: AuthedApp, identifier: str, password: str, **kwargs):
    return app.client.post(LOGIN, json={"identifier": identifier, "password": password}, **kwargs)


# ── login ────────────────────────────────────────────────────────────


def test_login_by_name_returns_a_token_pair(authed_app: AuthedApp) -> None:
    resp = _login(authed_app, "alice", authed_app.password)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["pincer_user_id"] == "alice"
    assert body["expires_in"] == 1800
    assert credentials.decode_token("j" * 48, body["access_token"], typ="access").sub == "alice"
    assert credentials.decode_token("j" * 48, body["refresh_token"], typ="refresh").sub == "alice"


@pytest.mark.parametrize("identifier", ["alice@example.com", "ALICE@Example.COM", "  alice@example.com  "])
def test_login_by_email_ignores_case_and_padding(authed_app: AuthedApp, identifier: str) -> None:
    resp = _login(authed_app, identifier, authed_app.password)
    assert resp.status_code == 200
    assert resp.json()["pincer_user_id"] == "alice"


def test_an_identity_name_is_matched_exactly(authed_app: AuthedApp) -> None:
    assert _login(authed_app, "Alice", authed_app.password).status_code == 401


def test_every_login_failure_looks_the_same(authed_app: AuthedApp) -> None:
    async def seed() -> None:
        await authed_app.identities.create_profile("keyonly", email="keyonly@example.com")
        await authed_app.auth.generate_api_key("keyonly")
        await authed_app.identities.create_profile("nothing")

    authed_app.run(seed)

    failures = [
        _login(authed_app, "alice", "wrong password"),
        _login(authed_app, "nobody", authed_app.password),
        _login(authed_app, "nobody@example.com", authed_app.password),
        _login(authed_app, "keyonly", authed_app.password),  # has a key, no password
        _login(authed_app, "nothing", authed_app.password),  # has no credentials at all
    ]

    assert {r.status_code for r in failures} == {401}
    assert {r.text for r in failures} == {failures[0].text}
    assert failures[0].json() == {"error": "invalid_credentials", "detail": "Invalid credentials"}
    assert failures[0].headers["www-authenticate"] == "Bearer"


def test_an_email_shared_by_two_identities_names_nobody(authed_app: AuthedApp) -> None:
    async def seed() -> None:
        await authed_app.identities.create_profile("alice2", email="Alice@Example.com")
        await authed_app.auth.set_password("alice2", "another password")

    authed_app.run(seed)

    assert _login(authed_app, "alice@example.com", authed_app.password).status_code == 401
    assert _login(authed_app, "alice@example.com", "another password").status_code == 401
    # Each of them still gets in by name.
    assert _login(authed_app, "alice", authed_app.password).status_code == 200
    assert _login(authed_app, "alice2", "another password").status_code == 200


def test_an_api_key_is_not_a_password(authed_app: AuthedApp) -> None:
    assert _login(authed_app, "alice", authed_app.api_key).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"identifier": "alice"},
        {"identifier": "", "password": "x"},
        {"identifier": "alice", "password": ""},
        {"identifier": "a" * 321, "password": "x"},
    ],
)
def test_malformed_login_is_a_422(authed_app: AuthedApp, body: dict[str, str]) -> None:
    assert authed_app.client.post(LOGIN, json=body).status_code == 422


def test_an_oversized_password_is_refused_without_being_echoed(authed_app: AuthedApp) -> None:
    # A pydantic length error would put the rejected input in the 422 body.
    oversized = "hunter2" * 100
    resp = _login(authed_app, "alice", oversized)
    assert resp.status_code == 401
    assert "hunter2" not in resp.text


def test_login_upgrades_a_hash_made_with_weaker_parameters(authed_app: AuthedApp, monkeypatch) -> None:
    async def stored_hash() -> tuple[str, int]:
        async with session_scope(authed_app.db_url) as session:
            row = await IdentityCredentialRepository(session).get("alice")
            return row.password_hash, row.token_version

    old_hash, old_version = authed_app.run(stored_hash)
    # The deployment's parameters are raised after alice's hash was made.
    monkeypatch.setattr(credentials, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1))

    assert _login(authed_app, "alice", authed_app.password).status_code == 200

    new_hash, new_version = authed_app.run(stored_hash)
    assert new_hash != old_hash
    assert "m=16,t=2" in new_hash
    assert new_version == old_version  # a re-hash signs nobody out
    assert _login(authed_app, "alice", authed_app.password).status_code == 200


# ── refresh ──────────────────────────────────────────────────────────


def test_refresh_returns_a_new_pair(authed_app: AuthedApp) -> None:
    resp = authed_app.client.post(REFRESH, json={"refresh_token": authed_app.refresh_token})

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["pincer_user_id"] == "alice"
    assert body["access_token"] != authed_app.access_token
    assert credentials.decode_token("j" * 48, body["access_token"], typ="access").sub == "alice"


def test_an_access_token_or_api_key_cannot_be_used_to_refresh(authed_app: AuthedApp) -> None:
    for value in (authed_app.access_token, authed_app.api_key, "garbage"):
        resp = authed_app.client.post(REFRESH, json={"refresh_token": value})
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_token"


def test_a_password_change_invalidates_the_refresh_token(authed_app: AuthedApp) -> None:
    authed_app.run(lambda: authed_app.auth.set_password("alice", "a brand new password"))

    resp = authed_app.client.post(REFRESH, json={"refresh_token": authed_app.refresh_token})

    assert resp.status_code == 401
    assert resp.json()["error"] == "token_expired"
    # The old password is gone too; the new one works.
    assert _login(authed_app, "alice", authed_app.password).status_code == 401
    assert _login(authed_app, "alice", "a brand new password").status_code == 200


def test_an_expired_refresh_token_is_token_expired(authed_app: AuthedApp) -> None:
    expired = credentials.encode_token("j" * 48, sub="alice", typ="refresh", ver=0, ttl_seconds=-10)
    resp = authed_app.client.post(REFRESH, json={"refresh_token": expired})
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_expired"


# ── throttling ───────────────────────────────────────────────────────


@pytest.fixture
def tight_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_AUTH_MAX_FAILURES", "3")
    monkeypatch.setenv("PINCER_AUTH_LOCKOUT_SECONDS", "60")


def test_login_is_throttled_per_account_across_ips(tight_budget: None, authed_app: AuthedApp) -> None:
    """X-Forwarded-For is whatever the client says it is, so an attacker who
    rotates it must still run out of guesses against one account."""
    for attempt in range(4):
        resp = _login(authed_app, "alice", "guess", headers={"X-Forwarded-For": f"10.0.0.{attempt}"})
        assert resp.status_code == 401

    locked = _login(authed_app, "Alice ", "guess", headers={"X-Forwarded-For": "10.0.9.9"})
    assert locked.status_code == 429
    assert locked.json()["error"] == "locked_out"
    assert int(locked.headers["retry-after"]) > 0
    # Even the right password waits out the lockout...
    assert _login(authed_app, "alice", authed_app.password, headers={"X-Forwarded-For": "10.0.9.8"}).status_code == 429

    # ...but only for that account.
    async def seed() -> None:
        await authed_app.identities.create_profile("bob")
        await authed_app.auth.set_password("bob", "bob's password")

    authed_app.run(seed)
    assert _login(authed_app, "bob", "bob's password", headers={"X-Forwarded-For": "10.0.9.7"}).status_code == 200


def test_login_is_throttled_per_ip_across_accounts(tight_budget: None, authed_app: AuthedApp) -> None:
    headers = {"X-Forwarded-For": "10.1.1.1"}
    for attempt in range(4):
        assert _login(authed_app, f"user{attempt}", "guess", headers=headers).status_code == 401

    assert _login(authed_app, "alice", authed_app.password, headers=headers).status_code == 429
    assert _login(authed_app, "alice", authed_app.password, headers={"X-Forwarded-For": "10.1.1.2"}).status_code == 200


def test_a_successful_login_clears_the_account_s_failures(tight_budget: None, authed_app: AuthedApp) -> None:
    for _ in range(3):
        assert _login(authed_app, "alice", "guess").status_code == 401
    assert _login(authed_app, "alice", authed_app.password).status_code == 200
    for _ in range(3):
        assert _login(authed_app, "alice", "guess").status_code == 401
    assert _login(authed_app, "alice", authed_app.password).status_code == 200


def test_garbage_refresh_tokens_spend_the_ip_budget_but_expired_ones_do_not(
    tight_budget: None, authed_app: AuthedApp
) -> None:
    expired = credentials.encode_token("j" * 48, sub="alice", typ="refresh", ver=0, ttl_seconds=-10)
    for _ in range(10):
        assert authed_app.client.post(REFRESH, json={"refresh_token": expired}).status_code == 401
    assert authed_app.client.post(REFRESH, json={"refresh_token": authed_app.refresh_token}).status_code == 200

    for _ in range(4):
        assert authed_app.client.post(REFRESH, json={"refresh_token": "garbage"}).status_code == 401
    assert authed_app.client.post(REFRESH, json={"refresh_token": authed_app.refresh_token}).status_code == 429


# ── the service ──────────────────────────────────────────────────────


def test_bearer_resolution(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        by_jwt = await auth.authenticate_bearer(authed_app.access_token)
        assert (by_jwt.pincer_user_id, by_jwt.method, by_jwt.interactive) == ("alice", "jwt", True)
        by_key = await auth.authenticate_bearer(authed_app.api_key)
        assert (by_key.pincer_user_id, by_key.method, by_key.interactive) == ("alice", "api_key", False)

        for bad in ("", "garbage", "pnc_not-a-real-key", authed_app.refresh_token):
            with pytest.raises(InvalidTokenError):
                await auth.authenticate_bearer(bad)

        expired = credentials.encode_token("j" * 48, sub="alice", typ="access", ver=0, ttl_seconds=-10)
        with pytest.raises(TokenStaleError):
            await auth.authenticate_bearer(expired)
        gone = credentials.encode_token("j" * 48, sub="deleted", typ="access", ver=0, ttl_seconds=60)
        with pytest.raises(TokenStaleError):
            await auth.authenticate_bearer(gone)

    authed_app.run(check)


def test_a_password_change_invalidates_access_tokens_but_not_the_api_key(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        pair = await auth.change_password("alice", authed_app.password, "a brand new password")
        with pytest.raises(TokenStaleError):
            await auth.authenticate_bearer(authed_app.access_token)
        assert (await auth.authenticate_bearer(pair.access_token)).pincer_user_id == "alice"
        assert (await auth.refresh(pair.refresh_token)).pincer_user_id == "alice"
        assert (await auth.authenticate_bearer(authed_app.api_key)).pincer_user_id == "alice"

        with pytest.raises(InvalidCredentialsError):
            await auth.change_password("alice", authed_app.password, "yet another password")
        with pytest.raises(ValueError, match="at least"):
            await auth.change_password("alice", "a brand new password", "short")

    authed_app.run(check)


def test_api_key_is_generated_once_and_replaced_only_by_force(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        info = await auth.api_key_info("alice")
        assert info.masked == f"{authed_app.api_key[:8]}…{authed_app.api_key[-4:]}"
        assert info.created_at

        with pytest.raises(ApiKeyExistsError):
            await auth.generate_api_key("alice")
        assert (await auth.authenticate_bearer(authed_app.api_key)).pincer_user_id == "alice"

        replacement, new_info = await auth.generate_api_key("alice", force=True)
        assert replacement != authed_app.api_key
        assert new_info.masked.endswith(replacement[-4:])
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_bearer(authed_app.api_key)
        assert (await auth.authenticate_bearer(replacement)).method == "api_key"

        # Only the hash is stored.
        async with session_scope(authed_app.db_url) as session:
            row = await IdentityCredentialRepository(session).get("alice")
        assert replacement not in repr(row)
        assert row.api_key_hash == credentials.hash_api_key(replacement)

    authed_app.run(check)


def test_credentials_need_an_existing_identity(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        with pytest.raises(UnknownIdentityError):
            await auth.set_password("ghost", "a long enough password")
        with pytest.raises(UnknownIdentityError):
            await auth.generate_api_key("ghost")
        assert await auth.api_key_info("ghost") is None
        assert await auth.identities_with_credentials() == 1

    authed_app.run(check)


def test_listen_in_ticket_is_bound_to_its_call(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        ticket = await auth.issue_ws_ticket("alice", "CA1")
        assert (await auth.authenticate_ws_ticket(ticket, "CA1")).pincer_user_id == "alice"
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_ws_ticket(ticket, "CA2")
        # A ticket is not a session, and a session is not a ticket.
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_bearer(ticket)
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_ws_ticket(authed_app.access_token, "CA1")

        await auth.set_password("alice", "a brand new password")
        with pytest.raises(TokenStaleError):
            await auth.authenticate_ws_ticket(ticket, "CA1")

    authed_app.run(check)
