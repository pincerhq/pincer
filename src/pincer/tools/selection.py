"""
ToolSelector — keeps each chat turn under the provider's tool cap (tool RAG).

When the registry holds more than ``tool_selection_max`` tools, a turn gets,
in this order and without duplicates:

1. the ``tool_selection_default`` tools (names or fnmatch patterns),
2. every LLM-visible tool of the memory MCP server (``memory_mcp_server``),
3. tools already called in the conversation,
4. the tools that best match the user's message, ranked by the tool index
   on the memory server (``tool_index_sync``/``tool_index_search``).

The index is synced in the background, so a cold start (model download, first
embedding of every tool) never blocks a turn: until it is ready, and whenever
ranking fails, 1-3 are followed by the rest in registry order.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.mcp.bridge import result_json

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pincer.config import Settings
    from pincer.mcp.manager import MCPClientManager

logger = logging.getLogger(__name__)

_SEARCH_TOOL = "tool_index_search"
_SYNC_TOOL = "tool_index_sync"
_INDEX_DESCRIPTION_CHARS = 1000


class _NoToolIndex(Exception):
    """The memory server is connected but has no tool index (an older image)."""


def _index_text(schema: dict[str, Any]) -> str:
    name = schema["name"].replace("__", " ").replace("_", " ")
    description = (schema.get("description") or "")[:_INDEX_DESCRIPTION_CHARS]
    return f"{name}. {description}"


def _index_version(schemas: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for name, description in sorted((s["name"], s.get("description") or "") for s in schemas):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(description.encode())
        digest.update(b"\0")
    return digest.hexdigest()


class ToolSelector:
    """Picks at most ``tool_selection_max`` tool schemas for a chat turn."""

    def __init__(
        self,
        mcp_manager: MCPClientManager | None,
        settings: Settings,
        *,
        timeout: float = 3.0,
    ) -> None:
        self._mcp_manager = mcp_manager
        self._settings = settings
        self._server = settings.memory_mcp_server
        self._timeout = timeout
        self._synced_version: str | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_task_version: str | None = None
        # Sync failures repeat on every turn until fixed; warn once per distinct cause.
        self._last_sync_error: str | None = None
        self._no_index_logged = False

    # ── Public API ────────────────────────────────────────

    def start_sync(self, schemas: list[dict[str, Any]]) -> None:
        """Warm the index up in the background (no-op below the limit or without a server)."""
        if self._mcp_manager is not None and len(schemas) > self._settings.tool_selection_max:
            self._ensure_sync_task(_index_version(schemas), schemas)

    async def select(
        self,
        schemas: list[dict[str, Any]],
        query: str,
        recent_tool_names: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        limit = self._settings.tool_selection_max
        if len(schemas) <= limit:
            return schemas

        by_name = {s["name"]: s for s in schemas}
        head = self._head(schemas, recent_tool_names)
        if len(head) > limit:
            logger.warning(
                "Default, memory and recent tools (%d) exceed tool_selection_max=%d — dropping: %s",
                len(head),
                limit,
                ", ".join(head[limit:]),
            )
            head = head[:limit]

        error: str | None = None
        ranked: list[str] = []
        if self._mcp_manager is not None and query.strip():
            try:
                ranked = await asyncio.wait_for(self._ranked(schemas, query, limit), self._timeout)
            except _NoToolIndex:
                # Checked per turn, so a server that connects or is upgraded later starts ranking.
                if not self._no_index_logged:
                    logger.info(
                        "MCP server '%s' has no tool index — default, memory and recent tools lead, "
                        "the rest follow in registry order",
                        self._server,
                    )
                    self._no_index_logged = True
            except TimeoutError:
                error = "tool index timed out"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        names = list(dict.fromkeys(head + [n for n in ranked if n in by_name]))
        if error is not None or not ranked:
            names = list(dict.fromkeys(names + list(by_name)))
        names = names[:limit]

        if error is not None:
            kept = set(names)
            logger.warning(
                "Tool selection fell back to registry order (%s) — sending %d of %d tools, dropping: %s",
                error,
                len(names),
                len(schemas),
                ", ".join(n for n in by_name if n not in kept),
            )
        return [by_name[n] for n in names]

    # ── Selection ─────────────────────────────────────────

    def _head(self, schemas: list[dict[str, Any]], recent_tool_names: Iterable[str]) -> list[str]:
        registered = [s["name"] for s in schemas]
        registered_set = set(registered)
        head: list[str] = []
        for pattern in self._settings.tool_selection_default:
            head.extend(n for n in registered if fnmatch.fnmatchcase(n, pattern))
        if self._mcp_manager is not None:
            head.extend(n for n in self._mcp_manager.tool_names(self._server) if n in registered_set)
        head.extend(n for n in recent_tool_names if n in registered_set)
        return list(dict.fromkeys(head))

    async def _ranked(self, schemas: list[dict[str, Any]], query: str, k: int) -> list[str]:
        version = _index_version(schemas)
        await self._ensure_synced(version, schemas)
        for attempt in range(2):
            data = await self._call(_SEARCH_TOOL, {"query": query, "k": k, "version": version})
            if not data.get("stale"):
                return [r["name"] for r in data.get("results", [])]
            if attempt == 0:
                # The server restarted (its index is in memory) or another sync won the race.
                self._synced_version = None
                await self._ensure_synced(version, schemas)
        raise RuntimeError("tool index still stale after re-sync")

    # ── Index sync ────────────────────────────────────────

    async def _ensure_synced(self, version: str, schemas: list[dict[str, Any]]) -> None:
        if self._synced_version == version:
            return
        # asyncio.wait, not await: a turn that stops waiting must not cancel the sync for the next one.
        task = self._ensure_sync_task(version, schemas)
        await asyncio.wait({task})
        task.result()

    def _ensure_sync_task(self, version: str, schemas: list[dict[str, Any]]) -> asyncio.Task[None]:
        task = self._sync_task
        if task is None or task.done() or self._sync_task_version != version:
            task = asyncio.create_task(self._sync(version, schemas, previous=task))
            task.add_done_callback(self._log_sync_failure)
            self._sync_task = task
            self._sync_task_version = version
        return task

    async def _sync(
        self,
        version: str,
        schemas: list[dict[str, Any]],
        previous: asyncio.Task[None] | None,
    ) -> None:
        if previous is not None and not previous.done():
            # Keep syncs ordered so the server ends at the newest version.
            await asyncio.wait({previous})
        tools = [{"name": s["name"], "text": _index_text(s)} for s in schemas]
        stats = await self._call(_SYNC_TOOL, {"tools": tools, "version": version})
        self._synced_version = version
        logger.info("Tool index synced: %s", stats)

    def _log_sync_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            self._last_sync_error = None
            return
        if isinstance(exc, _NoToolIndex):
            return
        cause = f"{type(exc).__name__}: {exc}"
        if cause != self._last_sync_error:
            logger.warning("Tool index sync on MCP server '%s' failed: %s", self._server, cause)
            self._last_sync_error = cause
        else:
            logger.debug("Tool index sync failed again: %s", cause)

    async def _call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._mcp_manager is None:
            raise RuntimeError("no tool index server")
        session = await self._mcp_manager.get_session(self._server)
        if not any(t.name == _SEARCH_TOOL for t in session.tools):
            raise _NoToolIndex
        result = await session.call_tool(tool_name, args)
        if getattr(result, "isError", False):
            content = getattr(result, "content", None) or []
            raise RuntimeError(f"{tool_name} failed: {getattr(content[0], 'text', '') if content else ''}")
        data = result_json(result)
        if not isinstance(data, dict):
            raise RuntimeError(f"{tool_name} returned {type(data).__name__}, expected an object")
        return data
