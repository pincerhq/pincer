"""Conversations API — memory-backed conversation list and retrieval."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request

from pincer.memory.base import (
    BaseMemoryBackend,
    Memory,
    PINCER_MEMORY_USER_TAG_PREFIX,
    PINCER_MEMORY_USER_REQUEST_PREFIX,
    PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _ts_to_iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _channel_tag(channel: str, channel_user_id: str) -> str:
    f"""Build the compound channel tag: {PINCER_MEMORY_USER_TAG_PREFIX}:{channel}:{channel_user_id}."""
    return f"{PINCER_MEMORY_USER_TAG_PREFIX}:{channel}:{channel_user_id}"


def _parse_messages(content: str) -> list[dict[str, Any]]:
    """Parse memory content into a [user, assistant] message pair.

    Tries JSON first: {"user": "...", "assistant": "..."}.
    Falls back to prefix-based plain-text parsing using the standard memory
    prefixes (PINCER_MEMORY_USER_REQUEST_PREFIX / PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX).
    Returns a single user message if neither format matches.
    """
    # 1. JSON path
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            messages: list[dict[str, Any]] = []
            if "user" in data:
                messages.append({"role": "user", "content": str(data["user"])})
            if "assistant" in data:
                messages.append({"role": "assistant", "content": str(data["assistant"])})
            if messages:
                return messages
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Plain-text prefix path: "User asked: ...\nAssistant replied: ..."
    user_prefix = f"{PINCER_MEMORY_USER_REQUEST_PREFIX}:"
    assistant_prefix = f"{PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX}:"
    user_idx = content.find(user_prefix)
    assistant_idx = content.find(assistant_prefix)
    messages = []
    if user_idx != -1:
        user_end = assistant_idx if assistant_idx > user_idx else len(content)
        messages.append({"role": "user", "content": content[user_idx + len(user_prefix):user_end].strip()})
    if assistant_idx != -1:
        messages.append({"role": "assistant", "content": content[assistant_idx + len(assistant_prefix):].strip()})
    if messages:
        return messages

    # 3. Fallback: treat the whole content as a user message
    return [{"role": "user", "content": content}]


def _preview(mem: Memory) -> str:
    with suppress(json.JSONDecodeError, TypeError):
        data = json.loads(mem.content)
        if isinstance(data, dict) and "user" in data:
            return str(data["user"])[:200]
    return mem.content[:200]


@asynccontextmanager
async def _open_backend(request: Request) -> AsyncIterator[BaseMemoryBackend]:
    """Yield the active memory backend.

    Reuses the agent's already-initialised backend when available (avoids
    opening a second DB connection). Falls back to a fresh backend built from
    settings for standalone / test deployments.
    """
    agent = getattr(request.app.state, "agent", None)
    backend: BaseMemoryBackend | None = getattr(agent, "_memory", None) if agent else None
    if backend is not None:
        yield backend
        return

    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    if settings.memory_backend == "mcp":
        from pincer.memory.mcp import MCPMemoryBackend

        store: BaseMemoryBackend = MCPMemoryBackend(server_name=settings.memory_mcp_server)
    else:
        from pincer.memory.sqlite import SQLiteMemoryBackend

        store = SQLiteMemoryBackend(settings.db_path)

    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@router.get("")
async def list_conversations(
    request: Request,
    user_id: str = Query(..., description="User ID to filter memories by"),
    channel: str | None = Query(default=None, description="Channel type (e.g. telegram, whatsapp, discord)"),
    channel_user_id: str | None = Query(default=None, description="Channel-specific user or chat ID"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    f"""List memory records for a user with optional offset pagination.

    When both *channel* and *channel_user_id* are provided, results are
    narrowed to the tag ``{PINCER_MEMORY_USER_TAG_PREFIX}{channel}:{channel_user_id}``.
    """
    tags: list[str] | None = None
    if channel and channel_user_id:
        tags = [_channel_tag(channel, channel_user_id)]
    try:
        async with _open_backend(request) as store:
            memories = await store.list_memories(
                user_id=user_id,
                limit=limit,
                offset=offset,
                category="exchange",
                tags=tags,
                match_all_tags=True,
            )
    except Exception as e:
        logger.error(str(e))
        return {"conversations": [], "total": 0, "limit": limit, "offset": offset}

    items = [
        {
            "id": mem.id,
            "user_id": mem.user_id,
            "category": mem.category,
            "tags": mem.tags,
            "preview": _preview(mem),
            "messages": _parse_messages(mem.content),
            "created_at": _ts_to_iso(mem.created_at),
        }
        for mem in memories
    ]
    return {"conversations": items, "total": len(items), "limit": limit, "offset": offset}


@router.get("/{conv_id}")
async def get_conversation(conv_id: str, request: Request) -> dict[str, Any]:
    """Fetch a single memory record and return its content as user + assistant messages."""
    try:
        async with _open_backend(request) as store:
            mem = await store.get_memory(conv_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Memory backend unavailable") from exc

    if mem is None or mem.category != "exchange":
        raise HTTPException(status_code=404, detail="Memory not found")

    return {
        "id": mem.id,
        "user_id": mem.user_id,
        "category": mem.category,
        "tags": mem.tags,
        "messages": _parse_messages(mem.content),
        "created_at": _ts_to_iso(mem.created_at),
    }
