"""Tests for the /api/identity endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.api.identity import router
from pincer.channels.base import ChannelType
from pincer.core.identity import IdentityResolver


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest_asyncio.fixture
async def resolver(tmp_path: Path) -> AsyncIterator[IdentityResolver]:
    db_path = tmp_path / "pincer.db"
    r = IdentityResolver(db_path, identity_map_config="")
    await r.ensure_table()
    yield r


def _fake_settings(db_path: str) -> Any:
    settings = type("Settings", (), {})()
    settings.db_path = db_path
    return settings


@pytest.mark.asyncio
class TestIdentityApi:
    async def test_list_includes_active_channel_fields(self, client: TestClient, resolver: IdentityResolver) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567890")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
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

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
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

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get(f"/api/identity/{uid}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_channel"] == "whatsapp"
        assert data["active_channel_updated_at"] is not None

    async def test_list_includes_timezone(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 77777)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute("UPDATE identity_meta SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid))
            await db.commit()

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get("/api/identity")

        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["timezone"] == "Europe/Berlin"

    async def test_get_single_identity_includes_timezone(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 88888)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute("UPDATE identity_meta SET timezone = ? WHERE pincer_user_id = ?", ("Europe/Berlin", uid))
            await db.commit()

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get(f"/api/identity/{uid}")

        assert resp.json()["timezone"] == "Europe/Berlin"

    async def test_list_includes_email(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 79797)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute("UPDATE identity_meta SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid))
            await db.commit()

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get("/api/identity")

        data = resp.json()
        entry = next(i for i in data["identities"] if i["pincer_user_id"] == uid)
        assert entry["email"] == "jane@example.com"

    async def test_get_single_identity_includes_email(self, client: TestClient, resolver: IdentityResolver) -> None:
        import aiosqlite

        uid = await resolver.resolve(ChannelType.TELEGRAM, 89898)
        async with aiosqlite.connect(resolver._db_path) as db:
            await db.execute("UPDATE identity_meta SET email = ? WHERE pincer_user_id = ?", ("jane@example.com", uid))
            await db.commit()

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get(f"/api/identity/{uid}")

        assert resp.json()["email"] == "jane@example.com"

    async def test_search_includes_active_channel_fields(self, client: TestClient, resolver: IdentityResolver) -> None:
        uid = await resolver.resolve(ChannelType.TELEGRAM, 66666)
        await resolver.touch_active_channel(uid, ChannelType.TELEGRAM)

        with patch("pincer.api.identity.get_settings_relaxed", return_value=_fake_settings(resolver._db_path)):
            resp = client.get("/api/identity", params={"search": "66666"})

        data = resp.json()
        assert len(data["identities"]) == 1
        assert data["identities"][0]["active_channel"] == "telegram"
