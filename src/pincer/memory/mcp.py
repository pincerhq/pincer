"""MCP-backed memory backend.

Delegates all storage to an external sqlite-vec-memory MCP server via tool calls.
user_id and category are encoded as tags: ["user:{user_id}", "category:{category}"].

Limitations vs SQLiteMemoryBackend:
- search_similar() is not supported (MCP server computes its own embeddings)
- update_memory() is not supported (tag preservation requires a GET, which is unavailable)
- count() is approximate (fetches up to 10 000 records and counts client-side)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.memory.base import (
    PINCER_MEMORY_CATEGORY_TAG_PREFIX,
    PINCER_MEMORY_COUNT_FETCH_LIMIT,
    PINCER_MEMORY_USER_TAG_PREFIX,
    BaseMemoryBackend,
    Memory,
)

if TYPE_CHECKING:
    from pincer.mcp.manager import MCPClientManager

logger = logging.getLogger(__name__)


def _parse_timestamp(s: str) -> float:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()
    except Exception:
        return 0.0


def _tags_for(user_id: str, category: str) -> list[str]:
    return [f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}", f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}"]


def _user_from_tags(tags: list[str]) -> str:
    return next(
        (
            t[len(f"{PINCER_MEMORY_USER_TAG_PREFIX}:") :]
            for t in tags
            if t.startswith(f"{PINCER_MEMORY_USER_TAG_PREFIX}:")
        ),
        "",
    )


def _category_from_tags(tags: list[str]) -> str:
    return next(
        (
            t[len(f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:") :]
            for t in tags
            if t.startswith(f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:")
        ),
        "general",
    )


def _row_to_memory(r: dict[str, Any], score: float = 0.0) -> Memory:
    tags: list[str] = r.get("tags", [])
    return Memory(
        id=str(r["id"]),
        user_id=_user_from_tags(tags),
        content=r["content"],
        category=_category_from_tags(tags),
        created_at=_parse_timestamp(r.get("created_at", "")),
        score=score,
        tags=tags,
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
        logger.info(args)
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

    async def get_memory(self, memory_id: str) -> Memory | None:
        try:
            row = await self._call_json("memory_get", {"memory_id": memory_id})
        except (ValueError, RuntimeError):
            return None
        if not row:
            return None
        return _row_to_memory(row)

    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = 10,
        offset: int = 0,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> list[Memory]:
        args: dict[str, Any] = {"limit": limit, "offset": offset}
        filter_tags: list[str] = []
        if user_id:
            filter_tags.append(f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}")
        if category:
            filter_tags.append(f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}")
        if tags:
            filter_tags.extend(tags)
        if filter_tags:
            args["tags"] = filter_tags
            # Use server-side AND logic when explicitly requested or when
            # multiple filter dimensions are combined (e.g. user + category).
            if match_all_tags or len(filter_tags) > 1:
                args["match_all_tags"] = True

        rows = await self._call_json("memory_list", args)
        return [_row_to_memory(r) for r in rows]

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if content is None:
            raise ValueError("MCPMemoryBackend.update_memory: content is required")
        # Build updated tags: caller-supplied tags take precedence; otherwise derive from category.
        # user_id tag cannot be recovered without a GET — callers should prefer delete + store for full updates.
        updated_tags: list[str] | None = tags
        if updated_tags is None and category is not None:
            updated_tags = [f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}"]
        await self._call("memory_update", {"memory_id": memory_id, "content": content, "tags": updated_tags})

    async def delete_memory(self, memory_id: str) -> None:
        await self._call("memory_delete", {"memory_id": memory_id})

    async def delete_user_memories(self, user_id: str) -> int:
        tag = f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}"
        rows = await self._call_json("memory_list", {"limit": PINCER_MEMORY_COUNT_FETCH_LIMIT, "tags": tag})
        deleted = 0
        for r in rows:
            try:
                await self._call("memory_delete", {"memory_id": r["id"]})
                deleted += 1
            except Exception:
                logger.warning("Failed to delete memory %s for user %s", r["id"], user_id, exc_info=True)
        return deleted

    async def count(
        self,
        user_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> int:
        args: dict[str, Any] = {"limit": PINCER_MEMORY_COUNT_FETCH_LIMIT}
        filter_tags: list[str] = []
        if user_id:
            filter_tags.append(f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}")
        if category:
            filter_tags.append(f"{PINCER_MEMORY_CATEGORY_TAG_PREFIX}:{category}")
        if tags:
            filter_tags.extend(tags)
        if filter_tags:
            args["tags"] = filter_tags
            if match_all_tags or len(filter_tags) > 1:
                args["match_all_tags"] = True
        rows = await self._call_json("memory_list", args)
        return len(rows)

    # ── Search ────────────────────────────────────────────

    async def search_text(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 5,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        # Tag filtering is done server-side; pass user tag so the server
        # fetches extra KNN candidates and filters before returning.
        args: dict[str, Any] = {"query": query, "limit": limit}
        filter_tags: list[str] = []
        if user_id:
            filter_tags.append(f"{PINCER_MEMORY_USER_TAG_PREFIX}:{user_id}")
        if tags:
            filter_tags.extend(tags)
        if filter_tags:
            args["tags"] = filter_tags
        rows = await self._call_json("memory_search", args)
        return [_row_to_memory(r, score=max(0.0, 1.0 - r.get("distance", 0.0))) for r in rows]
