"""Cost Dashboard API — FastAPI endpoints for spending data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from pincer.llm.cost_tracker import get_cost_tracker

router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/today")
async def get_today_costs() -> dict[str, Any]:
    tracker = await get_cost_tracker()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    costs = await tracker.get_daily_costs(today)
    budget = await tracker.get_budget_status()
    return {
        "date": today,
        "total_usd": costs["total"],
        "by_model": costs["by_model"],
        "by_tool": costs["by_tool"],
        "request_count": costs["request_count"],
        "budget": {
            "daily_limit": budget["daily_limit"],
            "spent_pct": budget["spent_pct"],
            "remaining": budget["remaining"],
            "is_downgraded": budget["is_downgraded"],
        },
    }


@router.get("/history")
async def get_cost_history(
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    tracker = await get_cost_tracker()
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    history = await tracker.get_daily_history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    total_usd = sum(e["total"] for e in history)
    total_requests = sum(e["requests"] for e in history)
    return {
        "period_days": days,
        "data": [
            {
                "date": e["date"],
                "total_usd": e["total"],
                "request_count": e["requests"],
            }
            for e in history
        ],
        "totals": {
            "total_usd": round(total_usd, 6),
            "total_requests": total_requests,
            "avg_daily_usd": round(total_usd / max(len(history), 1), 6),
        },
    }


@router.get("/by-tool")
async def get_costs_by_tool(
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    tracker = await get_cost_tracker()
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    breakdown = await tracker.get_costs_by_tool(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    return {
        "period_days": days,
        "tools": [
            {
                "tool": e["tool"],
                "total_usd": e["total"],
                "call_count": e["calls"],
                "avg_cost": e["total"] / max(e["calls"], 1),
            }
            for e in breakdown
        ],
    }


@router.get("/by-model")
async def get_costs_by_model(
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    tracker = await get_cost_tracker()
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    breakdown = await tracker.get_costs_by_model(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    return {
        "period_days": days,
        "models": [
            {
                "model": e["model"],
                "total_usd": e["total"],
                "request_count": e["requests"],
                "total_tokens": e["tokens"],
                "avg_cost_per_request": round(
                    e["total"] / max(e["requests"], 1), 6
                ),
            }
            for e in breakdown
        ],
    }
