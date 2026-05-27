"""Tests for new/updated SQLiteMemoryBackend methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from pincer.memory.sqlite import SQLiteMemoryBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> SQLiteMemoryBackend:
    backend = SQLiteMemoryBackend(tmp_path / "test.db")
    await backend.initialize()
    yield backend  # type: ignore[misc]
    await backend.close()


# ── get_memory ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_memory_returns_none_for_missing(store: SQLiteMemoryBackend) -> None:
    result = await store.get_memory("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_memory_returns_stored_record(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "hello world", category="exchange")
    result = await store.get_memory(mem_id)
    assert result is not None
    assert result.id == mem_id
    assert result.content == "hello world"
    assert result.category == "exchange"
    assert result.user_id == "user1"


@pytest.mark.asyncio
async def test_get_memory_includes_tags(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "tagged", extra_tags=["user:telegram:123"])
    result = await store.get_memory(mem_id)
    assert result is not None
    assert "user:telegram:123" in result.tags


# ── update_memory ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_memory_content(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "original content")
    await store.update_memory(mem_id, content="updated content")
    result = await store.get_memory(mem_id)
    assert result is not None
    assert result.content == "updated content"


@pytest.mark.asyncio
async def test_update_memory_category(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "some text", category="general")
    await store.update_memory(mem_id, category="exchange")
    result = await store.get_memory(mem_id)
    assert result is not None
    assert result.category == "exchange"


@pytest.mark.asyncio
async def test_update_memory_tags(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "tagged content")
    await store.update_memory(mem_id, tags=["custom:tag1", "custom:tag2"])
    result = await store.get_memory(mem_id)
    assert result is not None
    assert "custom:tag1" in result.tags
    assert "custom:tag2" in result.tags


@pytest.mark.asyncio
async def test_update_memory_no_fields_is_noop(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "unchanged")
    await store.update_memory(mem_id)
    result = await store.get_memory(mem_id)
    assert result is not None
    assert result.content == "unchanged"


# ── delete_memory ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_memory_removes_record(store: SQLiteMemoryBackend) -> None:
    mem_id = await store.store_memory("user1", "to be deleted")
    await store.delete_memory(mem_id)
    result = await store.get_memory(mem_id)
    assert result is None


@pytest.mark.asyncio
async def test_delete_memory_nonexistent_is_noop(store: SQLiteMemoryBackend) -> None:
    await store.delete_memory("does-not-exist")


# ── count ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_empty(store: SQLiteMemoryBackend) -> None:
    assert await store.count() == 0


@pytest.mark.asyncio
async def test_count_all(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "a")
    await store.store_memory("user2", "b")
    assert await store.count() == 2


@pytest.mark.asyncio
async def test_count_per_user(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "a")
    await store.store_memory("user1", "b")
    await store.store_memory("user2", "c")
    assert await store.count("user1") == 2
    assert await store.count("user2") == 1


@pytest.mark.asyncio
async def test_count_by_category(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "a", category="exchange")
    await store.store_memory("user1", "b", category="exchange")
    await store.store_memory("user1", "c", category="profile")
    assert await store.count(category="exchange") == 2
    assert await store.count(category="profile") == 1


@pytest.mark.asyncio
async def test_count_user_and_category(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "a", category="exchange")
    await store.store_memory("user1", "b", category="profile")
    await store.store_memory("user2", "c", category="exchange")
    assert await store.count(user_id="user1", category="exchange") == 1
    assert await store.count(user_id="user2", category="exchange") == 1


@pytest.mark.asyncio
async def test_count_tags_or_logic(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "tg", extra_tags=["user:telegram:111"])
    await store.store_memory("user1", "wa", extra_tags=["user:whatsapp:222"])
    await store.store_memory("user1", "plain")
    # OR: either tag matches — should count 2
    assert await store.count(tags=["user:telegram:111", "user:whatsapp:222"]) == 2


@pytest.mark.asyncio
async def test_count_tags_and_logic(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "both tags", category="exchange", extra_tags=["user:telegram:123"])
    await store.store_memory("user1", "only category", category="exchange")

    # AND: record must have user:user1, category:exchange AND user:telegram:123
    assert (
        await store.count(
            user_id="user1",
            category="exchange",
            tags=["user:telegram:123"],
            match_all_tags=True,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_count_tags_and_logic_no_match(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "wrong channel", category="exchange", extra_tags=["user:whatsapp:999"])
    assert (
        await store.count(
            user_id="user1",
            category="exchange",
            tags=["user:telegram:123"],
            match_all_tags=True,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_count_unknown_tag_returns_zero(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "something")
    assert await store.count(tags=["nonexistent:tag"]) == 0


# ── delete_user_memories ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_memories_returns_count(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "x")
    await store.store_memory("user1", "y")
    await store.store_memory("user2", "z")
    deleted = await store.delete_user_memories("user1")
    assert deleted == 2


@pytest.mark.asyncio
async def test_delete_user_memories_removes_only_target_user(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "x")
    await store.store_memory("user2", "z")
    await store.delete_user_memories("user1")
    assert await store.count("user1") == 0
    assert await store.count("user2") == 1


@pytest.mark.asyncio
async def test_delete_user_memories_no_records_returns_zero(store: SQLiteMemoryBackend) -> None:
    deleted = await store.delete_user_memories("nobody")
    assert deleted == 0


# ── list_memories: OR logic (default) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_memories_or_tags_matches_any(store: SQLiteMemoryBackend) -> None:
    id1 = await store.store_memory("user1", "telegram msg", category="exchange", extra_tags=["user:telegram:111"])
    id2 = await store.store_memory("user1", "whatsapp msg", category="exchange", extra_tags=["user:whatsapp:222"])
    id3 = await store.store_memory("user1", "profile info", category="profile")

    results = await store.list_memories(
        user_id="user1",
        tags=["user:telegram:111", "user:whatsapp:222"],
        match_all_tags=False,
    )
    ids = {m.id for m in results}
    assert id1 in ids
    assert id2 in ids
    assert id3 not in ids


# ── list_memories: AND logic (match_all_tags=True) ────────────────────────────


@pytest.mark.asyncio
async def test_list_memories_and_tags_returns_only_exact_matches(store: SQLiteMemoryBackend) -> None:
    id_with_channel = await store.store_memory(
        "user1",
        "has channel tag",
        category="exchange",
        extra_tags=["user:telegram:123"],
    )
    id_no_channel = await store.store_memory(
        "user1",
        "no channel tag",
        category="exchange",
    )

    results = await store.list_memories(
        user_id="user1",
        category="exchange",
        tags=["user:telegram:123"],
        match_all_tags=True,
    )
    ids = {m.id for m in results}
    assert id_with_channel in ids
    assert id_no_channel not in ids


@pytest.mark.asyncio
async def test_list_memories_and_tags_excludes_wrong_channel(store: SQLiteMemoryBackend) -> None:
    id_tg = await store.store_memory(
        "user1",
        "telegram record",
        category="exchange",
        extra_tags=["user:telegram:correct"],
    )
    id_wa = await store.store_memory(
        "user1",
        "whatsapp record",
        category="exchange",
        extra_tags=["user:whatsapp:wrong"],
    )

    results = await store.list_memories(
        user_id="user1",
        category="exchange",
        tags=["user:telegram:correct"],
        match_all_tags=True,
    )
    ids = {m.id for m in results}
    assert id_tg in ids
    assert id_wa not in ids


# ── list_memories: offset pagination ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_memories_offset_pagination(store: SQLiteMemoryBackend) -> None:
    for i in range(5):
        await store.store_memory("user1", f"memory {i}")

    page1 = await store.list_memories(user_id="user1", limit=3, offset=0)
    page2 = await store.list_memories(user_id="user1", limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 2
    assert {m.id for m in page1}.isdisjoint({m.id for m in page2})


@pytest.mark.asyncio
async def test_list_memories_category_filter(store: SQLiteMemoryBackend) -> None:
    ex_id = await store.store_memory("user1", "exchange record", category="exchange")
    await store.store_memory("user1", "general record", category="general")

    results = await store.list_memories(user_id="user1", category="exchange")
    assert all(m.category == "exchange" for m in results)
    assert any(m.id == ex_id for m in results)


@pytest.mark.asyncio
async def test_list_memories_no_filters_returns_all(store: SQLiteMemoryBackend) -> None:
    await store.store_memory("user1", "a")
    await store.store_memory("user2", "b")
    results = await store.list_memories()
    assert len(results) == 2
