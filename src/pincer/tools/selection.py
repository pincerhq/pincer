"""
ToolSelector — keeps each chat turn under the provider's tool cap (tool RAG).

OpenAI-wire providers reject a request with more than 128 tools; Anthropic has
no cap and caches the tools block as a static prefix, so selection only applies
when the default or a failover provider is OpenAI-wire. Then, when the registry
holds more than ``tool_selection_max`` tools, a turn gets, without duplicates:

1. the ``tool_selection_default`` tools (names or fnmatch patterns),
2. every LLM-visible tool of the memory MCP server (``memory_mcp_server``),
3. tools already called in the conversation,
4. the tools that best match the request, ranked by the tool index on the
   memory server (``tool_index_sync``/``tool_index_search``).

The chosen tools are sent in registry order, so the same set is always the
same prefix. The index syncs in the background, so a cold start (model
download, first embedding of every tool) never blocks a turn: until it is
ready, and whenever ranking is unavailable, 1-3 are followed by the rest in
registry order. Each fallback cause is logged once, then at debug.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.mcp.results import raise_on_error, result_json

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pincer.config import Settings
    from pincer.mcp.manager import MCPClientManager

logger = logging.getLogger(__name__)

_SEARCH_TOOL = "tool_index_search"
_SYNC_TOOL = "tool_index_sync"
_INDEX_DESCRIPTION_CHARS = 1000
# One search, plus one retry after re-syncing an index the server lost (it is in memory).
_SEARCH_ATTEMPTS = 2


class _Unranked(Exception):
    """Ranking is unavailable this turn for an expected reason; ``level`` is how loudly to say so once."""

    def __init__(self, reason: str, level: int) -> None:
        super().__init__(reason)
        self.level = level


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


def _has_tool_cap(settings: Settings) -> bool:
    """Whether any provider a turn may reach (default or failover) is OpenAI-wire, i.e. capped at 128 tools."""
    from pincer.llm.router import is_openai_wire

    return any(is_openai_wire(settings, name) for name in (settings.default_provider, *settings.fallback_providers))


class ToolSelector:
    """Picks at most ``tool_selection_max`` tool schemas for a chat turn."""

    def __init__(self, mcp_manager: MCPClientManager | None, settings: Settings) -> None:
        self._mcp_manager = mcp_manager
        self._settings = settings
        self._server = settings.memory_mcp_server
        self._applies = _has_tool_cap(settings)
        self._synced_version: str | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_task_version: str | None = None
        # Causes repeat on every turn until fixed; each is logged once, then at debug.
        self._last_sync_error: str | None = None
        self._last_fallback: str | None = None

    # ── Public API ────────────────────────────────────────

    def start_sync(self, schemas: list[dict[str, Any]]) -> None:
        """Warm the index up in the background (no-op when selection doesn't apply or without a server)."""
        if self._applies and self._mcp_manager is not None and len(schemas) > self._settings.tool_selection_max:
            self._ensure_sync_task(_index_version(schemas), schemas)

    async def select(
        self,
        schemas: list[dict[str, Any]],
        query: str,
        recent_tool_names: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        limit = self._settings.tool_selection_max
        if not self._applies or len(schemas) <= limit:
            return schemas

        registered = {s["name"] for s in schemas}
        head = self._head(schemas, recent_tool_names)
        if len(head) > limit:
            logger.warning(
                "Default, memory and recent tools (%d) exceed tool_selection_max=%d — dropping: %s",
                len(head),
                limit,
                ", ".join(head[limit:]),
            )
            head = head[:limit]

        fallback: _Unranked | None = None
        ranked: list[str] = []
        if not query.strip():
            fallback = _Unranked("no text to rank on", logging.DEBUG)
        elif self._mcp_manager is None:
            fallback = _Unranked("no memory MCP server", logging.DEBUG)
        else:
            timeout = self._settings.tool_selection_timeout
            try:
                ranked = await asyncio.wait_for(self._ranked(schemas, query, limit), timeout)
            except _Unranked as e:
                fallback = e
            except TimeoutError:
                fallback = _Unranked(f"tool index timed out after {timeout:g}s", logging.WARNING)
            except Exception as e:
                fallback = _Unranked(f"{type(e).__name__}: {e}", logging.WARNING)

        # Whatever the ranking leaves free (all of it, without ranking) is filled in registry order.
        names = list(dict.fromkeys(head + [n for n in ranked if n in registered] + [s["name"] for s in schemas]))
        chosen = set(names[:limit])
        selected = [s for s in schemas if s["name"] in chosen]

        if fallback is None:
            self._last_fallback = None
        else:
            self._log_fallback(fallback, [s["name"] for s in schemas if s["name"] not in chosen], len(schemas))
        return selected

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
        await self._index_session()  # not connected / no index: no sync task, no index call
        version = _index_version(schemas)
        if self._synced_version != version:
            task = self._sync_task
            if task is not None and not task.done() and self._sync_task_version == version:
                # Started earlier (startup warm-up, or a turn that stopped waiting): a long
                # first build must not hold every turn up to the timeout.
                raise _Unranked("tool index is still being built", logging.INFO)
            await self._sync_now(version, schemas)
        for attempt in range(_SEARCH_ATTEMPTS):
            data = await self._call(_SEARCH_TOOL, {"query": query, "k": k, "version": version})
            if not data.get("stale"):
                return [r["name"] for r in data.get("results", [])]
            # The server restarted (its index is in memory) or another sync won the race.
            self._synced_version = None
            if attempt + 1 < _SEARCH_ATTEMPTS:
                await self._sync_now(version, schemas)
        raise RuntimeError("tool index still stale after re-sync")

    def _log_fallback(self, cause: _Unranked, dropped: list[str], total: int) -> None:
        reason = str(cause)
        level = cause.level if reason != self._last_fallback else logging.DEBUG
        self._last_fallback = reason
        logger.log(
            level,
            "Tool selection without ranking (%s) — sending %d of %d tools, the rest in registry order; dropping: %s",
            reason,
            total - len(dropped),
            total,
            ", ".join(dropped),
        )

    # ── Index sync ────────────────────────────────────────

    async def _sync_now(self, version: str, schemas: list[dict[str, Any]]) -> None:
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
        if isinstance(exc, _Unranked):
            return  # not connected / no index: said once by the turn's fallback log
        cause = f"{type(exc).__name__}: {exc}"
        if cause != self._last_sync_error:
            logger.warning("Tool index sync on MCP server '%s' failed: %s", self._server, cause)
            self._last_sync_error = cause
        else:
            logger.debug("Tool index sync failed again: %s", cause)

    async def _index_session(self) -> Any:
        """The memory server's session, when it is connected and has the tool index."""
        assert self._mcp_manager is not None  # callers check
        try:
            session = await self._mcp_manager.get_session(self._server)
        except ValueError:
            raise _Unranked(f"MCP server '{self._server}' is not connected", logging.WARNING) from None
        if not any(t.name == _SEARCH_TOOL for t in session.tools):
            raise _Unranked(f"MCP server '{self._server}' has no tool index (older image)", logging.INFO)
        return session

    async def _call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        session = await self._index_session()
        data = result_json(raise_on_error(await session.call_tool(tool_name, args), tool_name))
        if not isinstance(data, dict):
            raise RuntimeError(f"{tool_name} returned {type(data).__name__}, expected an object")
        return data
