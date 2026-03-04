"""Audit API — FastAPI endpoints for audit log and stats."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Query

from pincer.security.audit import AuditAction, get_audit_logger

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _row_to_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Map DB row to frontend AuditEntry shape."""
    metadata = {}
    if row.get("metadata_json"):
        with suppress(json.JSONDecodeError, TypeError):
            metadata = json.loads(row["metadata_json"])
    return {
        "id": str(row.get("id", "")),
        "timestamp": row.get("timestamp", ""),
        "user_id": row.get("user_id", ""),
        "action": row.get("action", ""),
        "tool": row.get("tool"),
        "input_summary": row.get("input_summary"),
        "output_summary": row.get("output_summary"),
        "approved": bool(row.get("approved", 1)),
        "cost_usd": row.get("cost_usd"),
        "duration_ms": row.get("duration_ms"),
        "metadata": metadata,
    }


@router.get("")
async def get_audit(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None),
    user: str | None = Query(default=None, alias="user_id"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> dict[str, Any]:
    """List audit log entries with optional filters."""
    audit_action = None
    if action:
        with suppress(ValueError):
            audit_action = AuditAction(action)
    logger = await get_audit_logger()
    rows = await logger.query(
        user_id=user,
        action=audit_action,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    entries = [_row_to_entry(row) for row in rows]
    return {"entries": entries, "total": len(entries)}


@router.get("/stats")
async def get_audit_stats(
    since: str | None = Query(default=None, description="ISO date for 'today' filter"),
) -> dict[str, Any]:
    """Get aggregate audit statistics."""
    logger = await get_audit_logger()
    stats = await logger.get_stats(since=since)
    return {
        "total_entries": stats.get("total_entries", 0),
        "by_action": stats.get("by_action", {}),
        "by_tool": stats.get("by_tool", {}),
        "total_cost_usd": stats.get("total_cost_usd", 0.0),
        "failed_actions": stats.get("failed_actions", 0),
    }
