"""Tests for the /api/identity endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.api.identity import router
from pincer.channels.base import ChannelType
from pincer.core.identity import IdentityResolver
from pincer.db.engine import to_async_url
from pincer.services.identity import IdentityService, get_identity_service


@pytest_asyncio.fixture
async def resolver(tmp_path: Path) -> AsyncIterator[IdentityResolver]:
    db_path = tmp_path / "pincer.db"
    r = IdentityResolver(db_path, identity_map_config="")
    await r.ensure_table()
    yield r


@pytest.fixture
def client(resolver: IdentityResolver) -> TestClient:
    """The routes read the same database the `resolver` fixture writes to."""
    app = FastAPI()
    app.include_router(router)
    service = IdentityService(to_async_url(f"sqlite:///{resolver._db_path}"))
    app.dependency_overrides[get_identity_service] = lambda: service
    return TestClient(app)


@pytest.mark.asyncio
class TestIdentityApi:
    async def test_list_includes_active_channel_fields(self, client: TestClient, resolver: IdentityResolver) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567890")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        resp = client.get("/api/identity")

        assert resp.status_code == 200
        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["active_channel"] == "whatsapp"
        assert entry["active_channel_updated_at"] is not None
        assert entry["preferred_channel"] == "telegram"

    async def test_list_active_channel_null_when_untouched(
        self, client: TestClient, resolver: IdentityResolver
    ) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 99999)

        resp = client.get("/api/identity")

        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["active_channel"] is None
        assert entry["active_channel_updated_at"] is None

    async def test_get_single_identity_includes_active_channel(
        self, client: TestClient, resolver: IdentityResolver
    ) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55555)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567891")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        resp = client.get(f"/api/identity/{uid}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_channel"] == "whatsapp"
        assert data["active_channel_updated_at"] is not None

    async def test_list_includes_timezone(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 77777)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute(
                "UPDATE pincer_identity_profiles SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid)
            )
            await db.commit()

        resp = client.get("/api/identity")

        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["timezone"] == "Europe/Berlin"

    async def test_get_single_identity_includes_timezone(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 88888)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute(
                "UPDATE pincer_identity_profiles SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid)
            )
            await db.commit()

        resp = client.get(f"/api/identity/{uid}")

        assert resp.json()["timezone"] == "Europe/Berlin"

    async def test_list_includes_email(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 79797)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute(
                "UPDATE pincer_identity_profiles SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid)
            )
            await db.commit()

        resp = client.get("/api/identity")

        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["email"] == "jane@example.com"

    async def test_get_single_identity_includes_email(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 89898)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute(
                "UPDATE pincer_identity_profiles SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid)
            )
            await db.commit()

        resp = client.get(f"/api/identity/{uid}")

        assert resp.json()["email"] == "jane@example.com"

    async def test_search_includes_active_channel_fields(self, client: TestClient, resolver: IdentityResolver) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 66666)
        await resolver.touch_active_channel(uid, ChannelType.TELEGRAM)

        resp = client.get("/api/identity", params={"search": "66666"})

        data = resp.json()
        assert len(data["identities"]) == 1
        assert data["identities"][0]["active_channel"] == "telegram"


# ── /api/identity/me (issue #228) ────────────────────────────────────

ME = "/api/identity/me"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestMe:
    def test_me_is_the_authenticated_identity(self, authed_app) -> None:
        by_jwt = authed_app.client.get(ME, headers=authed_app.jwt_headers)
        assert by_jwt.status_code == 200
        body = by_jwt.json()
        assert (body["pincer_user_id"], body["email"], body["display_name"]) == ("alice", "alice@example.com", "Alice")
        assert body["auth_method"] == "jwt"
        assert body["channels"] == []

        by_key = authed_app.client.get(ME, headers=authed_app.api_key_headers)
        assert by_key.json()["auth_method"] == "api_key"

        assert authed_app.client.get(ME).status_code == 401

    def test_me_never_carries_credentials(self, authed_app) -> None:
        for path in (ME, "/api/identity", "/api/identity/alice"):
            text = authed_app.client.get(path, headers=authed_app.jwt_headers).text
            assert "argon2" not in text
            assert "password" not in text
            assert "api_key" not in text.replace('"auth_method":"api_key"', "")
            assert "token_version" not in text

    def test_me_is_not_shadowed_by_the_id_route(self, authed_app) -> None:
        """`/me` is declared before `/{pincer_user_id}`; nobody is called `me`."""
        assert authed_app.client.get(ME, headers=authed_app.jwt_headers).json()["pincer_user_id"] == "alice"
        assert authed_app.client.get("/api/identity/nobody", headers=authed_app.jwt_headers).status_code == 404


class TestMyPassword:
    def _change(self, authed_app, current: str, new: str, headers: dict[str, str] | None = None):
        return authed_app.client.put(
            f"{ME}/password",
            json={"current_password": current, "new_password": new},
            headers=headers or authed_app.jwt_headers,
        )

    def test_change_invalidates_old_tokens_and_returns_a_working_pair(self, authed_app) -> None:
        resp = self._change(authed_app, authed_app.password, "a brand new password")

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        pair = resp.json()
        assert pair["pincer_user_id"] == "alice"

        # The session that made the change, and its refresh token, are dead...
        stale = authed_app.client.get(ME, headers=authed_app.jwt_headers)
        assert stale.status_code == 401
        assert stale.json()["error"] == "token_expired"
        refresh = authed_app.client.post("/api/auth/refresh", json={"refresh_token": authed_app.refresh_token})
        assert refresh.status_code == 401
        # ...the returned pair works...
        assert authed_app.client.get(ME, headers=_bearer(pair["access_token"])).status_code == 200
        again = authed_app.client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert again.status_code == 200
        # ...the API key is untouched, and only the new password signs in.
        assert authed_app.client.get(ME, headers=authed_app.api_key_headers).status_code == 200
        login = "/api/auth/login"
        assert (
            authed_app.client.post(login, json={"identifier": "alice", "password": authed_app.password}).status_code
            == 401
        )
        assert (
            authed_app.client.post(login, json={"identifier": "alice", "password": "a brand new password"}).status_code
            == 200
        )

    def test_wrong_current_password_is_403_and_changes_nothing(self, authed_app) -> None:
        resp = self._change(authed_app, "not the password", "a brand new password")
        assert resp.status_code == 403
        # Not 401: the dashboard would take that for an expired session and retry.
        assert authed_app.client.get(ME, headers=authed_app.jwt_headers).status_code == 200

    def test_weak_new_password_is_422_without_echoing_it(self, authed_app) -> None:
        resp = self._change(authed_app, authed_app.password, "short")
        assert resp.status_code == 422
        assert "at least 8 characters" in resp.json()["detail"]
        oversized = self._change(authed_app, authed_app.password, "hunter2" * 100)
        assert oversized.status_code == 422
        assert "hunter2" not in oversized.text
        assert authed_app.client.get(ME, headers=authed_app.jwt_headers).status_code == 200

    def test_an_api_key_cannot_change_the_password(self, authed_app) -> None:
        resp = self._change(authed_app, authed_app.password, "a brand new password", authed_app.api_key_headers)
        assert resp.status_code == 403
        assert authed_app.client.get(ME, headers=authed_app.jwt_headers).status_code == 200

    def test_guessing_the_current_password_is_throttled(self, authed_app, monkeypatch) -> None:
        """A stolen session must not be a free oracle for the password."""
        guard = authed_app.client.app.state.login_guard
        monkeypatch.setattr(guard, "max_failures", 3)
        for _ in range(4):
            assert self._change(authed_app, "guess", "a brand new password").status_code == 403
        locked = self._change(authed_app, authed_app.password, "a brand new password")
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0


class TestMyApiKey:
    def test_masked_only_after_creation(self, authed_app) -> None:
        resp = authed_app.client.get(f"{ME}/api-key", headers=authed_app.jwt_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is True
        assert body["masked"] == f"{authed_app.api_key[:8]}…{authed_app.api_key[-4:]}"
        assert body["created_at"]
        assert authed_app.api_key not in resp.text
        # The key itself may ask too.
        assert authed_app.client.get(f"{ME}/api-key", headers=authed_app.api_key_headers).json() == body

    def test_no_key_yet(self, authed_app) -> None:
        async def seed() -> str:
            await authed_app.identities.create_profile("bob")
            await authed_app.auth.set_password("bob", "bob's password")
            return (await authed_app.auth.login("bob", "bob's password")).access_token

        bob = _bearer(authed_app.run(seed))
        assert authed_app.client.get(f"{ME}/api-key", headers=bob).json() == {
            "exists": False,
            "masked": None,
            "created_at": None,
        }

        created = authed_app.client.post(f"{ME}/api-key", headers=bob)
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        key = created.json()["api_key"]
        assert key.startswith("pnc_")
        assert created.json()["masked"].endswith(key[-4:])
        assert authed_app.client.get(ME, headers=_bearer(key)).json()["pincer_user_id"] == "bob"

    def test_once_then_409_then_force(self, authed_app) -> None:
        again = authed_app.client.post(f"{ME}/api-key", headers=authed_app.jwt_headers)
        assert again.status_code == 409
        assert "force" in again.json()["detail"]
        assert authed_app.client.get(ME, headers=authed_app.api_key_headers).status_code == 200  # still valid

        forced = authed_app.client.post(f"{ME}/api-key?force=true", headers=authed_app.jwt_headers)
        assert forced.status_code == 201
        replacement = forced.json()["api_key"]
        assert replacement != authed_app.api_key

        old = authed_app.client.get(ME, headers=authed_app.api_key_headers)
        assert old.status_code == 401
        assert authed_app.client.get(ME, headers=_bearer(replacement)).status_code == 200
        masked = authed_app.client.get(f"{ME}/api-key", headers=authed_app.jwt_headers)
        assert replacement not in masked.text
        assert masked.json()["masked"].endswith(replacement[-4:])

    def test_an_api_key_cannot_mint_its_own_replacement(self, authed_app) -> None:
        for suffix in ("", "?force=true"):
            resp = authed_app.client.post(f"{ME}/api-key{suffix}", headers=authed_app.api_key_headers)
            assert resp.status_code == 403
        assert authed_app.client.get(ME, headers=authed_app.api_key_headers).status_code == 200
