"""The cost API adds the cache token counts beside its existing fields (issue #357)."""

from __future__ import annotations

import time

import httpx
import pytest

from pincer.db.session import session_scope
from pincer.models.costs import CostLog
from pincer.repositories.costs import CostLogRepository
from pincer.services.costs import CostService, get_cost_service

# (model, cost, uncached input, output, cache read, 5-minute write, 1-hour write)
_ROWS = [
    ("claude-sonnet-5-5", 0.1093765, 36, 1_226, 281_470, 27_559, 0),
    ("claude-sonnet-5-5", 0.01, 10, 20, 1_000, 0, 300),
    ("gpt-5.4-mini", 0.00066, 200, 100, 800, 0, 0),
]


@pytest.fixture
async def api(migrated_db: str):
    """The real app, its cost service on a scratch database holding `_ROWS` from just now."""
    from pincer.api.server import create_app

    async with session_scope(migrated_db) as session:
        for model, cost, uncached, output, read, write_5m, write_1h in _ROWS:
            await CostLogRepository(session).add(
                CostLog(
                    timestamp=time.time(),
                    provider="test",
                    model=model,
                    input_tokens=uncached,
                    output_tokens=output,
                    cache_read_tokens=read,
                    cache_write_5m_tokens=write_5m,
                    cache_write_1h_tokens=write_1h,
                    cost_usd=cost,
                ),
                refresh=False,
            )
    app = create_app()
    app.dependency_overrides[get_cost_service] = lambda: CostService(migrated_db, daily_budget=5.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_by_model_adds_each_model_s_cache_counts(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/costs/by-model?days=1")
    assert response.status_code == 200
    assert response.json()["models"] == [
        {
            "model": "claude-sonnet-5-5",
            "total_usd": 0.119376,
            "request_count": 2,
            "total_tokens": 1_292,  # uncached input + output, as before
            "avg_cost_per_request": 0.059688,
            "cache_read_tokens": 282_470,
            "cache_write_5m_tokens": 27_559,
            "cache_write_1h_tokens": 300,
        },
        {
            "model": "gpt-5.4-mini",
            "total_usd": 0.00066,
            "request_count": 1,
            "total_tokens": 300,
            "avg_cost_per_request": 0.00066,
            "cache_read_tokens": 800,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
        },
    ]


async def test_today_adds_the_day_s_cache_totals(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/costs/today")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {
        "date",
        "total_usd",
        "by_model",
        "by_tool",
        "request_count",
        "budget",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
    }
    assert (data["cache_read_tokens"], data["cache_write_5m_tokens"], data["cache_write_1h_tokens"]) == (
        283_270,
        27_559,
        300,
    )
    assert (data["request_count"], data["by_model"]) == (3, {"claude-sonnet-5-5": 0.119376, "gpt-5.4-mini": 0.00066})
