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
LOGOUT = "/api/auth/logout"
STATUS = "/api/status"


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

    locked = _login(authed_app, "alice", "guess", headers={"X-Forwarded-For": "10.0.9.9"})
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


def test_logins_sent_together_get_no_more_guesses_than_sequential_ones(authed_app: AuthedApp) -> None:
    """Checking the lockout and recording the failure are two steps with a
    password hash in between; requests that arrive together must not all
    pass the check before any of them has failed."""
    import asyncio

    import httpx

    app = authed_app.client.app
    budget = app.state.login_guard.max_failures
    assert budget == 10

    async def burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            guesses = [client.post(LOGIN, json={"identifier": "alice", "password": f"guess {n}"}) for n in range(60)]
            return [resp.status_code for resp in await asyncio.gather(*guesses)]

    statuses = authed_app.run(burst)

    assert set(statuses) == {401, 429}
    assert 1 <= statuses.count(401) <= budget + 1
    assert statuses.count(429) == 60 - statuses.count(401)
    # Nothing is left reserved once the burst is over.
    for guard in (app.state.auth_guard, app.state.login_guard):
        assert all(attempts.in_flight == 0 for attempts in guard._ips.values())


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
        session = await auth.authenticate_bearer(authed_app.access_token)
        ticket = await auth.issue_ws_ticket(session, "CA1")
        # The wrong call is refused without spending the ticket.
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_ws_ticket(ticket, "CA2")
        listener = await auth.authenticate_ws_ticket(ticket, "CA1")
        assert (listener.pincer_user_id, listener.interactive, listener.session_id) == (
            "alice",
            True,
            session.session_id,
        )
        # A ticket is not a session, and a session is not a ticket.
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_bearer(ticket)
        with pytest.raises(InvalidTokenError):
            await auth.authenticate_ws_ticket(authed_app.access_token, "CA1")

        unused = await auth.issue_ws_ticket(session, "CA1")
        await auth.set_password("alice", "a brand new password")
        with pytest.raises(TokenStaleError):
            await auth.authenticate_ws_ticket(unused, "CA1")

    authed_app.run(check)


def test_a_listen_in_ticket_opens_one_socket(authed_app: AuthedApp) -> None:
    """The ticket travels in a URL, so it is in an access log by the time it
    has been used. A second use must not open a second socket."""
    auth = authed_app.auth

    async def check() -> None:
        session = await auth.authenticate_bearer(authed_app.access_token)
        ticket = await auth.issue_ws_ticket(session, "CA1")
        assert (await auth.authenticate_ws_ticket(ticket, "CA1")).pincer_user_id == "alice"
        for _ in range(2):
            with pytest.raises(InvalidTokenError):
                await auth.authenticate_ws_ticket(ticket, "CA1")
        # Another ticket for the same call is its own ticket.
        again = await auth.issue_ws_ticket(session, "CA1")
        assert (await auth.authenticate_ws_ticket(again, "CA1")).pincer_user_id == "alice"

    authed_app.run(check)


def test_spent_listen_in_tickets_are_forgotten_once_they_have_expired(authed_app: AuthedApp, monkeypatch) -> None:
    import time as time_module

    auth = authed_app.auth

    async def check() -> None:
        session = await auth.authenticate_bearer(authed_app.access_token)
        for _ in range(3):
            await auth.authenticate_ws_ticket(await auth.issue_ws_ticket(session, "CA1"), "CA1")
        assert len(auth._spent_tickets) == 3

        later = time_module.time() + 120
        monkeypatch.setattr(time_module, "time", lambda: later)
        auth._spend_ticket("one-more", int(later) + 60)
        assert list(auth._spent_tickets) == ["one-more"]

    authed_app.run(check)


def test_a_listen_in_ticket_dies_with_the_session_that_asked_for_it(authed_app: AuthedApp) -> None:
    auth = authed_app.auth

    async def check() -> None:
        session = await auth.authenticate_bearer(authed_app.access_token)
        ticket = await auth.issue_ws_ticket(session, "CA1")
        await auth.logout(session)
        # Signed out, not forged: refused without spending the lockout budget.
        with pytest.raises(TokenStaleError):
            await auth.authenticate_ws_ticket(ticket, "CA1")

    authed_app.run(check)

    # A ticket that names no session at all opens nothing either.
    async def sessionless() -> None:
        async with session_scope(authed_app.db_url) as session:
            ver = (await IdentityCredentialRepository(session).get("alice")).token_version
        bare = credentials.encode_token("j" * 48, sub="alice", typ="ws", ver=ver, ttl_seconds=60, call="CA1")
        with pytest.raises(TokenStaleError):
            await auth.authenticate_ws_ticket(bare, "CA1")

    authed_app.run(sessionless)


# ── sessions: single-use refresh tokens, logout, revocation ──────────


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _refresh(app: AuthedApp, token: str):
    return app.client.post(REFRESH, json={"refresh_token": token})


def test_a_refresh_token_is_spent_by_using_it(authed_app: AuthedApp, monkeypatch) -> None:
    from pincer.services import auth as auth_service

    monkeypatch.setattr(auth_service, "REFRESH_REUSE_GRACE_SECONDS", -1)  # no grace: strict single use

    first = _refresh(authed_app, authed_app.refresh_token)
    assert first.status_code == 200
    rotated = first.json()

    # The copy someone kept is refused — and presenting it ends the session,
    # so the pair the legitimate client just received dies with it.
    replay = _refresh(authed_app, authed_app.refresh_token)
    assert replay.status_code == 401
    assert replay.json()["error"] == "token_expired"
    assert _refresh(authed_app, rotated["refresh_token"]).status_code == 401
    assert authed_app.client.get(STATUS, headers=_bearer(rotated["access_token"])).status_code == 401
    assert authed_app.client.get(STATUS, headers=authed_app.jwt_headers).status_code == 401
    # Signing in again is all it takes to recover.
    assert _login(authed_app, "alice", authed_app.password).status_code == 200


def test_two_tabs_refreshing_at_once_both_succeed(authed_app: AuthedApp) -> None:
    """Both tabs hold the same refresh token when the access token expires.
    The slower one arrives just after the rotation and must not be treated as
    a thief."""
    tab_a = _refresh(authed_app, authed_app.refresh_token)
    tab_b = _refresh(authed_app, authed_app.refresh_token)
    assert (tab_a.status_code, tab_b.status_code) == (200, 200)
    # The last pair written is the one the shared storage ends up holding.
    assert _refresh(authed_app, tab_b.json()["refresh_token"]).status_code == 200


def test_the_slower_tab_is_handed_the_token_the_faster_one_got(authed_app: AuthedApp) -> None:
    """Each tab keeps what it was answered with. If the slower tab rotated
    again, the faster tab's token would be two rotations old the next time it
    refreshed — outside the grace window, a "theft" that ends the session."""
    p1 = _refresh(authed_app, authed_app.refresh_token).json()
    p2 = _refresh(authed_app, authed_app.refresh_token).json()

    def jti(pair: dict) -> str | None:
        return credentials.decode_token("j" * 48, pair["refresh_token"], typ="refresh").jti

    assert jti(p1) == jti(p2)
    # Whichever tab refreshes next, and in whichever order, both stay signed in.
    for pair in (p1, p2, p2, p1):
        again = _refresh(authed_app, pair["refresh_token"])
        assert again.status_code == 200
        assert authed_app.client.get(STATUS, headers=_bearer(again.json()["access_token"])).status_code == 200


def test_the_grace_window_does_not_reach_back_two_rotations(authed_app: AuthedApp) -> None:
    first = _refresh(authed_app, authed_app.refresh_token).json()
    second = _refresh(authed_app, first["refresh_token"]).json()

    # Two rotations old: a copy someone kept, and the session ends with it.
    assert _refresh(authed_app, authed_app.refresh_token).status_code == 401
    assert _refresh(authed_app, second["refresh_token"]).status_code == 401


def test_refreshes_that_race_rotate_once_and_all_succeed(authed_app: AuthedApp) -> None:
    """The swap is a compare-and-set: of the refreshes that read the same
    current token, one rotates and the rest are handed its result."""
    import asyncio

    auth = authed_app.auth

    async def race() -> None:
        pairs = await asyncio.gather(*(auth.refresh(authed_app.refresh_token) for _ in range(5)))
        jtis = {credentials.decode_token("j" * 48, p.refresh_token, typ="refresh").jti for p in pairs}
        assert len(jtis) == 1
        for pair in pairs:
            assert (await auth.authenticate_bearer(pair.access_token)).pincer_user_id == "alice"
        assert (await auth.refresh(pairs[-1].refresh_token)).pincer_user_id == "alice"

    authed_app.run(race)


def test_a_refresh_that_loses_the_swap_takes_the_grace_path(authed_app: AuthedApp, monkeypatch) -> None:
    """Forced, since SQLite serialises the race away: the row is rotated by
    someone else between this refresh's read and its write."""
    from pincer.repositories.identity import AuthSessionRepository

    auth = authed_app.auth
    real_rotate = AuthSessionRepository.rotate
    stolen: list[str] = []

    async def rotate_after_a_rival(self, session_id, presented_jti, values):
        if not stolen:
            rival = {**values, "refresh_jti": "the-rival-s-token"}
            assert await real_rotate(self, session_id, presented_jti, rival) == 1
            stolen.append(rival["refresh_jti"])
        return await real_rotate(self, session_id, presented_jti, values)

    monkeypatch.setattr(AuthSessionRepository, "rotate", rotate_after_a_rival)

    async def check() -> None:
        pair = await auth.refresh(authed_app.refresh_token)
        assert credentials.decode_token("j" * 48, pair.refresh_token, typ="refresh").jti == "the-rival-s-token"
        assert (await auth.authenticate_bearer(pair.access_token)).pincer_user_id == "alice"

    authed_app.run(check)


def test_a_refresh_that_loses_to_a_logout_is_refused(authed_app: AuthedApp, monkeypatch) -> None:
    from pincer.repositories.identity import AuthSessionRepository

    auth = authed_app.auth

    async def rotate_after_a_logout(self, session_id, presented_jti, values):
        await self.delete_by_id(session_id)
        return 0

    monkeypatch.setattr(AuthSessionRepository, "rotate", rotate_after_a_logout)

    async def check() -> None:
        with pytest.raises(TokenStaleError):
            await auth.refresh(authed_app.refresh_token)

    authed_app.run(check)


def test_a_chain_of_refreshes_keeps_the_session_alive(authed_app: AuthedApp) -> None:
    token = authed_app.refresh_token
    for _ in range(4):
        resp = _refresh(authed_app, token)
        assert resp.status_code == 200
        token = resp.json()["refresh_token"]
        assert authed_app.client.get(STATUS, headers=_bearer(resp.json()["access_token"])).status_code == 200


def test_logout_ends_the_session_on_the_server(authed_app: AuthedApp) -> None:
    other = _login(authed_app, "alice", authed_app.password).json()  # a second browser

    assert authed_app.client.post(LOGOUT, headers=authed_app.jwt_headers).status_code == 204

    # A copy of either token is useless after logout, not merely forgotten by the client.
    dead = authed_app.client.get(STATUS, headers=authed_app.jwt_headers)
    assert dead.status_code == 401
    assert dead.json()["error"] == "token_expired"
    assert _refresh(authed_app, authed_app.refresh_token).status_code == 401
    # The other browser and the API key are untouched.
    assert authed_app.client.get(STATUS, headers=_bearer(other["access_token"])).status_code == 200
    assert _refresh(authed_app, other["refresh_token"]).status_code == 200
    assert authed_app.client.get(STATUS, headers=authed_app.api_key_headers).status_code == 200


def test_logout_needs_a_caller_and_is_harmless_for_an_api_key(authed_app: AuthedApp) -> None:
    assert authed_app.client.post(LOGOUT).status_code == 401
    assert authed_app.client.post(LOGOUT, headers=authed_app.api_key_headers).status_code == 204
    assert authed_app.client.get(STATUS, headers=authed_app.api_key_headers).status_code == 200
    assert authed_app.client.get(STATUS, headers=authed_app.jwt_headers).status_code == 200


def test_a_token_without_a_session_is_not_accepted(authed_app: AuthedApp) -> None:
    """Correctly signed and at the right version, but no server-side session."""

    async def version() -> int:
        async with session_scope(authed_app.db_url) as session:
            return (await IdentityCredentialRepository(session).get("alice")).token_version

    ver = authed_app.run(version)
    for sid in (None, "0198c1b2-0000-7000-8000-000000000000", "not-a-uuid"):
        token = credentials.encode_token("j" * 48, sub="alice", typ="access", ver=ver, ttl_seconds=60, sid=sid)
        resp = authed_app.client.get(STATUS, headers=_bearer(token))
        assert resp.status_code == 401, sid
        assert resp.json()["error"] == "token_expired"


def test_revoked_then_recreated_credentials_do_not_revive_old_tokens(authed_app: AuthedApp) -> None:
    """Revocation is a version comparison. Credentials made again under the
    same id must not start at the version the old tokens carry."""

    async def revoke_and_recreate() -> None:
        assert await authed_app.auth.revoke("alice")
        assert not await authed_app.auth.revoke("alice")
        await authed_app.auth.set_password("alice", "a completely new password")

    authed_app.run(revoke_and_recreate)

    assert authed_app.client.get(STATUS, headers=authed_app.jwt_headers).status_code == 401
    assert _refresh(authed_app, authed_app.refresh_token).status_code == 401
    assert authed_app.client.get(STATUS, headers=authed_app.api_key_headers).status_code == 401
    assert _login(authed_app, "alice", authed_app.password).status_code == 401
    assert _login(authed_app, "alice", "a completely new password").status_code == 200


def test_a_password_set_from_the_cli_ends_every_session(authed_app: AuthedApp) -> None:
    from pincer.repositories.identity import AuthSessionRepository

    _login(authed_app, "alice", authed_app.password)
    authed_app.run(lambda: authed_app.auth.set_password("alice", "a brand new password"))

    async def sessions() -> int:
        async with session_scope(authed_app.db_url) as session:
            return len(await AuthSessionRepository(session).list())

    assert authed_app.run(sessions) == 0


# ── one account, one budget ──────────────────────────────────────────


def test_name_and_email_share_one_failure_budget(tight_budget: None, authed_app: AuthedApp) -> None:
    """Otherwise every account has as many budgets as it has identifiers."""
    for attempt, identifier in enumerate(["alice", "alice@example.com", "ALICE@example.com", "alice"]):
        resp = _login(authed_app, identifier, "guess", headers={"X-Forwarded-For": f"10.2.0.{attempt}"})
        assert resp.status_code == 401
    for identifier in ("alice", "alice@example.com"):
        locked = _login(authed_app, identifier, authed_app.password, headers={"X-Forwarded-For": "10.2.9.9"})
        assert locked.status_code == 429, identifier


def test_login_failures_also_throttle_the_password_change(tight_budget: None, authed_app: AuthedApp) -> None:
    for attempt in range(4):
        _login(authed_app, "alice@example.com", "guess", headers={"X-Forwarded-For": f"10.3.0.{attempt}"})
    resp = authed_app.client.put(
        "/api/identity/me/password",
        json={"current_password": "guess", "new_password": "a brand new password"},
        headers=authed_app.jwt_headers,
    )
    assert resp.status_code == 429


def test_an_unknown_name_is_throttled_like_a_real_one(tight_budget: None, authed_app: AuthedApp) -> None:
    """The lockout must not say which names exist."""
    for attempt in range(4):
        resp = _login(authed_app, "Ghost", "guess", headers={"X-Forwarded-For": f"10.4.0.{attempt}"})
        assert resp.status_code == 401
    assert _login(authed_app, " ghost", "guess", headers={"X-Forwarded-For": "10.4.9.9"}).status_code == 429


def test_the_account_lockout_is_one_flat_window(tight_budget: None, authed_app: AuthedApp) -> None:
    """Anyone can spend someone else's budget, so one more wrong guess after
    a lockout must not double it towards an hour."""
    guard = authed_app.client.app.state.login_guard
    assert guard.escalate is False
    for attempt in range(4):
        _login(authed_app, "alice", "guess", headers={"X-Forwarded-For": f"10.5.0.{attempt}"})
    first = int(_login(authed_app, "alice", "guess", headers={"X-Forwarded-For": "10.5.9.9"}).headers["retry-after"])
    assert first <= 61

    # The window passes; the account has a full budget again.
    for attempts in guard._ips.values():
        attempts.locked_until = 0.001
    for attempt in range(3):
        resp = _login(authed_app, "alice", "guess", headers={"X-Forwarded-For": f"10.6.0.{attempt}"})
        assert resp.status_code == 401
    assert _login(authed_app, "alice", authed_app.password, headers={"X-Forwarded-For": "10.6.9.9"}).status_code == 200


def test_the_auth_service_is_built_once_per_app(authed_app: AuthedApp, monkeypatch) -> None:
    from pincer.services import auth as auth_service

    built = 0
    real = auth_service.build_auth_service

    def counting():
        nonlocal built
        built += 1
        return real()

    monkeypatch.setattr(auth_service, "build_auth_service", counting)
    for _ in range(3):
        assert authed_app.client.get(STATUS, headers=authed_app.jwt_headers).status_code == 200
        assert authed_app.client.get("/api/identity/me/api-key", headers=authed_app.jwt_headers).status_code == 200
    assert built == 1
