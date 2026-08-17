"""Schedules API — read-only listing of scheduled background tasks.

Surfaces `CronScheduler` (src/pincer/scheduler/cron.py) — until now only
reachable through chat tools (`tools/builtin/schedule_tool.py`) — as a REST
endpoint for the dashboard. Recurring schedules and not-yet-fired one-off
schedules are "future" (shown by default, ascending by next run); already
fired one-off schedules are "past" (opt-in via `include_past`, descending by
last run).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pincer.config import get_settings_relaxed
from pincer.scheduler.cron import CronScheduler, is_one_time_cron

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _row_to_task(row: dict[str, Any], timing: str) -> dict[str, Any]:
    action = row["action"]
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except (json.JSONDecodeError, TypeError):
            action = {}
    return {
        "id": row["id"],
        "pincer_user_id": row["pincer_user_id"],
        "name": row["name"],
        "kind": "one_time" if is_one_time_cron(row["cron_expr"]) else "recurring",
        "timing": timing,
        "cron_expr": row["cron_expr"],
        "timezone": row["timezone"],
        "channel": row["channel"],
        "enabled": bool(row["enabled"]),
        "action": action,
        "last_run_at": row["last_run_at"],
        "next_run_at": row["next_run_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("")
async def list_scheduled_tasks(
    include_past: bool = Query(default=False, description="Also include already-fired one-off tasks"),
) -> dict[str, Any]:
    """List scheduled tasks: recurring + upcoming by default, plus past one-offs on request."""
    try:
        settings = get_settings_relaxed()
        scheduler = CronScheduler(settings.db_path)
        await scheduler.ensure_table()
        rows = await scheduler.list_all()
    except Exception as e:
        logger.exception("Failed to list schedules")
        raise HTTPException(status_code=500, detail="Failed to list schedules") from e

    future_rows = []
    past_rows = []
    for row in rows:
        if is_one_time_cron(row["cron_expr"]) and row["last_run_at"] is not None:
            past_rows.append(row)
        else:
            future_rows.append(row)

    future_rows.sort(key=lambda r: r["next_run_at"] or "")
    past_rows.sort(key=lambda r: r["last_run_at"] or "", reverse=True)

    tasks = [_row_to_task(r, "future") for r in future_rows]
    if include_past:
        tasks += [_row_to_task(r, "past") for r in past_rows]

    return {
        "tasks": tasks,
        "total": len(tasks),
        "future_count": len(future_rows),
        "past_count": len(past_rows),
    }
