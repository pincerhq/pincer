"""Tests for cross-channel memory persistence."""

import pytest
import pytest_asyncio

from pincer.channels.base import ChannelType
from pincer.core.identity import IdentityResolver
from pincer.memory.store import MemoryStore


@pytest_asyncio.fixture
async def identity_resolver(tmp_path):
    db_path = tmp_path / "identity.db"
    r = IdentityResolver(
        db_path,
        identity_map_config="telegram:12345=whatsapp:491234567890",
    )
    await r.ensure_table()
    await r.seed_from_config()
    return r


@pytest_asyncio.fixture
async def memory_store(tmp_path):
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_store_on_telegram_recall_on_whatsapp(
    identity_resolver, memory_store
):
    """Store memory via Telegram identity, recall via WhatsApp identity."""
    uid_tg = await identity_resolver.resolve(ChannelType.TELEGRAM, 12345)
    uid_wa = await identity_resolver.resolve(ChannelType.WHATSAPP, "491234567890")
    assert uid_tg == uid_wa, f"Identity mismatch: {uid_tg} != {uid_wa}"

    await memory_store.store_memory(
        user_id=uid_tg,
        content="My favorite restaurant is Vapiano in Düsseldorf",
        category="general",
    )

    results = await memory_store.search_text(
        "favorite restaurant", user_id=uid_wa, limit=5
    )
    assert len(results) > 0, "Memory not found cross-channel"
    assert "Vapiano" in str(results), "Memory content mismatch"


@pytest.mark.asyncio
async def test_store_on_whatsapp_recall_on_telegram(
    identity_resolver, memory_store
):
    """Store memory via WhatsApp identity, recall via Telegram identity."""
    uid_wa = await identity_resolver.resolve(ChannelType.WHATSAPP, "491234567890")
    uid_tg = await identity_resolver.resolve(ChannelType.TELEGRAM, 12345)
    assert uid_tg == uid_wa

    await memory_store.store_memory(
        user_id=uid_wa,
        content="User prefers dark mode",
        category="preference",
    )

    results = await memory_store.search_text(
        "dark mode", user_id=uid_tg, limit=5
    )
    assert len(results) > 0
    assert "dark mode" in str(results).lower()
