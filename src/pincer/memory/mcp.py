"""MCP-backed memory backend.

Delegates all storage to an external sqlite-vec-memory MCP server via tool calls.
user_id and category are encoded as tags: ["user:{user_id}", "category:{category}"].

Limitations vs SQLiteMemoryBackend:
- get_memory(id) is not supported (returns None)
- search_similar() is not supported (MCP server computes its own embeddings)
- update_memory() is not supported (tag preservation requires a GET, which is unavailable)
- count() is approximate (fetches up to 10 000 records and counts client-side)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.memory.base import BaseMemoryBackend, Memory

if TYPE_CHECKING:
    from pincer.mcp.manager import MCPClientManager

logger = logging.getLogger(__name__)

_USER_TAG_PREFIX = "user:"
_CATEGORY_TAG_PREFIX = "category:"
_COUNT_FETCH_LIMIT = 10_000


def _parse_timestamp(s: str) -> float:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()
    except Exception:
        return 0.0


def _tags_for(user_id: str, category: str) -> list[str]:
    return [f"{_USER_TAG_PREFIX}{user_id}", f"{_CATEGORY_TAG_PREFIX}{category}"]


def _user_from_tags(tags: list[str]) -> str:
    return next((t[len(_USER_TAG_PREFIX) :] for t in tags if t.startswith(_USER_TAG_PREFIX)), "")


def _category_from_tags(tags: list[str]) -> str:
    return next((t[len(_CATEGORY_TAG_PREFIX) :] for t in tags if t.startswith(_CATEGORY_TAG_PREFIX)), "general")


def _row_to_memory(r: dict[str, Any], score: float = 0.0) -> Memory:
    tags = r.get("tags", [])
    return Memory(
        id=str(r["id"]),
        user_id=_user_from_tags(tags),
        content=r["content"],
        category=_category_from_tags(tags),
        created_at=_parse_timestamp(r.get("created_at", "")),
        score=score,
    )


class MCPMemoryBackend(BaseMemoryBackend):
    """Memory backend that calls an external sqlite-vec-memory MCP server."""

    def __init__(self, server_name: str = "sqlite-vec-memory") -> None:
        self._server_name = server_name
        self._mcp_manager: MCPClientManager | None = None

    def set_mcp_manager(self, manager: MCPClientManager) -> None:
        """Inject MCPClientManager after MCP servers have connected."""
        self._mcp_manager = manager

    async def initialize(self) -> None:
        logger.info("MCPMemoryBackend initialised (server=%s)", self._server_name)

    async def close(self) -> None:
        pass

    # ── Internal ──────────────────────────────────────────

    async def _call(self, tool_name: str, args: dict[str, Any]) -> str:
        if not self._mcp_manager:
            raise RuntimeError(
                "MCPMemoryBackend: MCP manager not set. Call set_mcp_manager() after MCP servers have started."
            )
        try:
            session = await self._mcp_manager.get_session(self._server_name)
        except ValueError as e:
            raise RuntimeError(f"MCPMemoryBackend: memory server '{self._server_name}' is not connected") from e
        result = await session.call_tool(tool_name, args)
        content = getattr(result, "content", [])
        return content[0].text if content else ""

    async def _call_json(self, tool_name: str, args: dict[str, Any]) -> Any:
        raw = await self._call(tool_name, args)
        return json.loads(raw)

    # ── Core CRUD ─────────────────────────────────────────

    async def store_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding: list[float] | None = None,
        extra_tags: list[str] | None = None,
    ) -> str:
        tags = _tags_for(user_id, category)
        if extra_tags:
            tags.extend(extra_tags)
        raw = await self._call("memory_store", {"content": content, "tags": tags})
        # response: "stored:{id}"
        return raw.split(":", 1)[-1].strip()

    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = 10,
        category: str | None = None,
    ) -> list[Memory]:
        args: dict[str, Any] = {"limit": limit}
        filter_tags: list[str] = []
        if user_id:
            filter_tags.append(f"{_USER_TAG_PREFIX}{user_id}")
        if category:
            filter_tags.append(f"{_CATEGORY_TAG_PREFIX}{category}")
        if filter_tags:
            args["tags"] = filter_tags

        rows = await self._call_json("memory_list", args)
        memories = [_row_to_memory(r) for r in rows]

        # When both user_id and category are given the server returns records
        # matching EITHER tag (OR logic); keep only those with both.
        if user_id and category:
            memories = [m for m in memories if m.user_id == user_id and m.category == category]

        return memories[:limit]

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
    ) -> None:
        if content is None:
            raise ValueError("MCPMemoryBackend.update_memory: content is required")
        # Tags cannot be reconstructed without a GET; only content is updated here.
        # The user_id tag will be lost — callers should prefer delete + store for full updates.
        tags: list[str] | None = None
        if category is not None:
            tags = [f"{_CATEGORY_TAG_PREFIX}{category}"]
        await self._call("memory_update", {"memory_id": int(memory_id), "content": content, "tags": tags})

    async def delete_memory(self, memory_id: str) -> None:
        await self._call("memory_delete", {"memory_id": int(memory_id)})

    async def delete_user_memories(self, user_id: str) -> None:
        tag = f"{_USER_TAG_PREFIX}{user_id}"
        rows = await self._call_json("memory_list", {"limit": _COUNT_FETCH_LIMIT, "tags": tag})
        for r in rows:
            try:
                await self._call("memory_delete", {"memory_id": r["id"]})
            except Exception:
                logger.warning("Failed to delete memory %s for user %s", r["id"], user_id, exc_info=True)

    async def count(self, user_id: str | None = None) -> int:
        args: dict[str, Any] = {"limit": _COUNT_FETCH_LIMIT}
        if user_id:
            args["tags"] = f"{_USER_TAG_PREFIX}{user_id}"
        rows = await self._call_json("memory_list", args)
        return len(rows)

    # ── Search ────────────────────────────────────────────

    async def search_text(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[Memory]:
        # Tag filtering is done server-side; pass user tag so the server
        # fetches extra KNN candidates and filters before returning.
        args: dict[str, Any] = {"query": query, "limit": limit}
        if user_id:
            args["tags"] = f"{_USER_TAG_PREFIX}{user_id}"
        rows = await self._call_json("memory_search", args)
        return [_row_to_memory(r, score=max(0.0, 1.0 - r.get("distance", 0.0))) for r in rows]
