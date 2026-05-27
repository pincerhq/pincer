"""Tests for MCPMemoryBackend."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.memory.base import (
    PINCER_MEMORY_CATEGORY_TAG_PREFIX,
    PINCER_MEMORY_USER_TAG_PREFIX,
)
from pincer.memory.mcp import MCPMemoryBackend


def _mock_manager(tool_responses: dict[str, object]) -> MagicMock:
    """Build a fake MCPClientManager that returns preset results per tool name."""
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        raw = tool_responses.get(name, "[]")
        result = MagicMock()
        result.content = [MagicMock(text=raw if isinstance(raw, str) else json.dumps(raw))]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    return manager


def _row(
    id: int = 1,
    content: str = "hello",
    user_id: str = "u1",
    category: str = "exchange",
    created_at: str = "2026-05-25T00:00:00+00:00",
    extra_tags: list[str] | None = None,
) -> dict:
    tags = [f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}", f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}"]
    if extra_tags:
        tags.extend(extra_tags)
    return {"id": id, "content": content, "tags": tags, "created_at": created_at}


@pytest.fixture
def backend() -> MCPMemoryBackend:
    return MCPMemoryBackend(server_name="memory-test")


# ── get_memory ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_memory_returns_none_when_manager_not_set(backend: MCPMemoryBackend) -> None:
    result = await backend.get_memory("42")
    assert result is None


@pytest.mark.asyncio
async def test_get_memory_returns_none_for_null_response(backend: MCPMemoryBackend) -> None:
    backend.set_mcp_manager(_mock_manager({"memory_get": "null"}))
    result = await backend.get_memory("42")
    assert result is None


@pytest.mark.asyncio
async def test_get_memory_returns_memory_on_hit(backend: MCPMemoryBackend) -> None:
    row = _row(id=42, content="a stored fact", user_id="u1", category="exchange")
    backend.set_mcp_manager(_mock_manager({"memory_get": json.dumps(row)}))
    result = await backend.get_memory("42")
    assert result is not None
    assert result.content == "a stored fact"
    assert result.user_id == "u1"
    assert result.category == "exchange"


@pytest.mark.asyncio
async def test_get_memory_returns_none_on_runtime_error_from_call(backend: MCPMemoryBackend) -> None:
    """If the MCP call raises RuntimeError (e.g. server not connected), get_memory returns None."""
    manager = AsyncMock()
    manager.get_session.side_effect = ValueError("server not connected")
    backend.set_mcp_manager(manager)
    result = await backend.get_memory("some-uuid")
    assert result is None


# ── list_memories ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_memories_passes_user_and_category_tags(backend: MCPMemoryBackend) -> None:
    rows = [_row()]
    captured: list[dict] = []
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append({"name": name, "args": args})
        result = MagicMock()
        result.content = [MagicMock(text=json.dumps(rows))]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    await backend.list_memories(user_id="u1", category="exchange")
    call_args = captured[0]["args"]
    assert f"{PINCER_MEMORY_USER_TAG_PREFIX}:u1" in call_args["tags"]
    assert f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:exchange" in call_args["tags"]


@pytest.mark.asyncio
async def test_list_memories_sets_match_all_when_multiple_tags(backend: MCPMemoryBackend) -> None:
    rows = [_row()]
    captured: list[dict] = []
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append(args)
        result = MagicMock()
        result.content = [MagicMock(text=json.dumps(rows))]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    # user + category = 2 filter tags → match_all should be set
    await backend.list_memories(user_id="u1", category="exchange")
    assert captured[0].get("match_all_tags") is True


@pytest.mark.asyncio
async def test_list_memories_sets_match_all_when_explicitly_requested(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append(args)
        result = MagicMock()
        result.content = [MagicMock(text="[]")]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    await backend.list_memories(user_id="u1", match_all_tags=True)
    assert captured[0].get("match_all_tags") is True


@pytest.mark.asyncio
async def test_list_memories_passes_offset(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append(args)
        result = MagicMock()
        result.content = [MagicMock(text="[]")]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    await backend.list_memories(user_id="u1", offset=5)
    assert captured[0]["offset"] == 5


@pytest.mark.asyncio
async def test_list_memories_returns_parsed_memories(backend: MCPMemoryBackend) -> None:
    rows = [_row(id=1, content="fact one"), _row(id=2, content="fact two")]
    backend.set_mcp_manager(_mock_manager({"memory_list": json.dumps(rows)}))
    results = await backend.list_memories(user_id="u1")
    assert len(results) == 2
    contents = {m.content for m in results}
    assert "fact one" in contents
    assert "fact two" in contents


# ── delete_user_memories ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_memories_returns_deleted_count(backend: MCPMemoryBackend) -> None:
    rows = [_row(id=1), _row(id=2)]
    delete_count = {"n": 0}
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        result = MagicMock()
        if name == "memory_list":
            result.content = [MagicMock(text=json.dumps(rows))]
        else:
            delete_count["n"] += 1
            result.content = [MagicMock(text="ok")]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    deleted = await backend.delete_user_memories("u1")
    assert deleted == 2
    assert delete_count["n"] == 2


@pytest.mark.asyncio
async def test_delete_user_memories_empty_returns_zero(backend: MCPMemoryBackend) -> None:
    backend.set_mcp_manager(_mock_manager({"memory_list": "[]"}))
    result = await backend.delete_user_memories("nobody")
    assert result == 0


# ── count ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_returns_length_of_list(backend: MCPMemoryBackend) -> None:
    rows = [_row(id=i) for i in range(7)]
    backend.set_mcp_manager(_mock_manager({"memory_list": json.dumps(rows)}))
    result = await backend.count()
    assert result == 7


@pytest.mark.asyncio
async def test_count_empty_returns_zero(backend: MCPMemoryBackend) -> None:
    backend.set_mcp_manager(_mock_manager({"memory_list": "[]"}))
    assert await backend.count() == 0


def _capturing_manager(rows: list, captured: list[dict]) -> MagicMock:
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append(args)
        result = MagicMock()
        result.content = [MagicMock(text=json.dumps(rows))]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    return manager


@pytest.mark.asyncio
async def test_count_user_encodes_user_tag(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    backend.set_mcp_manager(_capturing_manager([], captured))
    await backend.count(user_id="u1")
    assert f"{PINCER_MEMORY_USER_TAG_PREFIX}:u1" in captured[0]["tags"]


@pytest.mark.asyncio
async def test_count_category_encodes_category_tag(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    backend.set_mcp_manager(_capturing_manager([], captured))
    await backend.count(category="exchange")
    assert f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:exchange" in captured[0]["tags"]


@pytest.mark.asyncio
async def test_count_user_and_category_sets_match_all(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    backend.set_mcp_manager(_capturing_manager([], captured))
    await backend.count(user_id="u1", category="exchange")
    assert captured[0].get("match_all_tags") is True


@pytest.mark.asyncio
async def test_count_extra_tags_forwarded(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    backend.set_mcp_manager(_capturing_manager([], captured))
    await backend.count(user_id="u1", tags=["user:telegram:123"])
    tags_sent = captured[0]["tags"]
    assert f"{PINCER_MEMORY_USER_TAG_PREFIX}:u1" in tags_sent
    assert "user:telegram:123" in tags_sent


@pytest.mark.asyncio
async def test_count_match_all_tags_explicit(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    backend.set_mcp_manager(_capturing_manager([], captured))
    await backend.count(user_id="u1", tags=["user:telegram:123"], match_all_tags=True)
    assert captured[0].get("match_all_tags") is True


@pytest.mark.asyncio
async def test_count_returns_row_count_with_filters(backend: MCPMemoryBackend) -> None:
    rows = [_row(id=i) for i in range(3)]
    backend.set_mcp_manager(_mock_manager({"memory_list": json.dumps(rows)}))
    assert await backend.count(user_id="u1", category="exchange") == 3


# ── store_memory ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_memory_returns_id(backend: MCPMemoryBackend) -> None:
    backend.set_mcp_manager(_mock_manager({"memory_store": "stored:99"}))
    mem_id = await backend.store_memory("u1", "some content", category="general")
    assert mem_id == "99"


@pytest.mark.asyncio
async def test_store_memory_includes_extra_tags(backend: MCPMemoryBackend) -> None:
    captured: list[dict] = []
    session = AsyncMock()

    async def call_tool(name: str, args: dict) -> MagicMock:
        captured.append({"name": name, "args": args})
        result = MagicMock()
        result.content = [MagicMock(text="stored:5")]
        return result

    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend.set_mcp_manager(manager)

    await backend.store_memory("u1", "content", extra_tags=["user:telegram:123"])
    tags_sent = captured[0]["args"]["tags"]
    assert "user:telegram:123" in tags_sent
