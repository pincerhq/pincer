"""Conversations API — FastAPI endpoints for conversation list and detail."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from pincer.config import get_settings_relaxed

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _ts_to_iso(ts: float | None) -> str:
    """Convert Unix timestamp to ISO string."""
    if ts is None:
        return ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat()


def _msg_to_frontend(msg: dict[str, Any]) -> dict[str, Any]:
    """Map stored message to frontend Message shape."""
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", str(p)) for p in content if isinstance(p, dict)
        )
    return {
        "role": msg.get("role", "user"),
        "content": str(content),
        "timestamp": _ts_to_iso(msg.get("timestamp")),
        "tool_name": msg.get("tool_name"),
        "tool_input": msg.get("tool_input"),
    }


@router.get("")
async def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    channel: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    """List conversations with optional filters."""
    try:
        settings = get_settings_relaxed()
        db_path = settings.db_path
    except Exception:
        return {"conversations": [], "total": 0}

    conditions: list[str] = []
    params: list[Any] = []
    if channel:
        conditions.append("channel = ?")
        params.append(channel)
    if search:
        conditions.append("(user_id LIKE ? OR messages_json LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    conversations: list[dict[str, Any]] = []
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        sql = f"""
            SELECT id, user_id, channel, messages_json, created_at, updated_at
            FROM conversations {where}
            ORDER BY updated_at DESC LIMIT ?
        """
        async with db.execute(sql, params) as cursor:
            async for row in cursor:
                msgs = []
                try:
                    msgs = json.loads(row["messages_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    pass
                last_msg = ""
                if msgs:
                    last = msgs[-1]
                    content = last.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", str(p))
                            for p in content
                            if isinstance(p, dict)
                        )
                    last_msg = str(content)[:200]
                conversations.append({
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "channel": row["channel"],
                    "last_message": last_msg,
                    "message_count": len(msgs),
                    "created_at": _ts_to_iso(row["created_at"]),
                    "updated_at": _ts_to_iso(row["updated_at"]),
                })

    return {"conversations": conversations, "total": len(conversations)}


@router.get("/{conv_id}")
async def get_conversation(conv_id: str) -> dict[str, Any]:
    """Get a single conversation with messages."""
    try:
        settings = get_settings_relaxed()
        db_path = settings.db_path
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, user_id, channel, messages_json, created_at, updated_at "
            "FROM conversations WHERE id = ?",
            (conv_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = []
    try:
        msgs = json.loads(row["messages_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "channel": row["channel"],
        "messages": [_msg_to_frontend(m) for m in msgs if isinstance(m, dict)],
        "created_at": _ts_to_iso(row["created_at"]),
        "updated_at": _ts_to_iso(row["updated_at"]),
    }
