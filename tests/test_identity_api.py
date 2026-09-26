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
                "UPDATE identity_profiles SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid)
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
                "UPDATE identity_profiles SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid)
            )
            await db.commit()

        resp = client.get(f"/api/identity/{uid}")

        assert resp.json()["timezone"] == "Europe/Berlin"

    async def test_list_includes_email(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 79797)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute(
                "UPDATE identity_profiles SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid)
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
                "UPDATE identity_profiles SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid)
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
