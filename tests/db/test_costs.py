"""Cost accounting on the repository/service layer, on SQLite and Postgres."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.exceptions import BudgetExceededError
from pincer.models.costs import CostLog, ImageCostLog
from pincer.repositories.costs import CostLogRepository
from pincer.services.costs import CostService, calculate_cost

_TABLES = [CostLog.__table__, ImageCostLog.__table__]

# 2025-12-31T23:59:59Z and 2026-01-01T00:00:00Z: one second, two UTC days.
_LAST_SECOND_OF_2025 = 1_767_225_599.0
_FIRST_SECOND_OF_2026 = 1_767_225_600.0


@pytest.fixture
async def url(db_url: str):
    """The cost tables, created from the models (the drift test holds them to
    the migrations), and dropped again so Postgres runs stay independent."""
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: CostLog.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: CostLog.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: CostLog.metadata.drop_all(c, tables=_TABLES))


async def _log(url: str, *, at: float, model: str = "gpt-4o", cost: float = 1.0, tokens: tuple[int, int] = (10, 5)):
    async with session_scope(url) as session:
        await CostLogRepository(session).add(
            CostLog(
                timestamp=at,
                provider="test",
                model=model,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                cost_usd=cost,
            )
        )


async def test_record_prices_the_call_and_stores_it(url):
    service = CostService(url)
    cost = await service.record("openai", "gpt-4o", 1000, 500, session_id="s1")
    assert cost == pytest.approx(calculate_cost("gpt-4o", 1000, 500))
    summary = await service.get_summary()
    assert (summary.total_calls, summary.total_input_tokens, summary.total_output_tokens) == (1, 1000, 500)
    assert summary.total_usd == pytest.approx(cost)


async def test_a_free_provider_costs_nothing(url):
    assert await CostService(url).record("local", "gpt-4o", 1000, 500, is_free=True) == 0.0


async def test_going_over_budget_raises_and_records_nothing(url):
    service = CostService(url, daily_budget=0.001)
    with pytest.raises(BudgetExceededError):
        await service.record("openai", "gpt-4o", 1_000_000, 0)  # $2.50
    assert (await service.get_summary()).total_calls == 0


async def test_today_spend_counts_llm_and_images_but_not_yesterday(url):
    service = CostService(url)
    await _log(url, at=time.time() - 2 * 86400, cost=5.0)
    await _log(url, at=time.time(), cost=1.0)
    await service.add_image_cost(0.25, "fal")
    assert await service.get_today_spend() == pytest.approx(1.25)
    assert await service.get_image_count_today() == 1


async def test_history_groups_by_utc_day(url):
    await _log(url, at=_LAST_SECOND_OF_2025, cost=1.0)
    await _log(url, at=_FIRST_SECOND_OF_2026, cost=2.0)
    await _log(url, at=_FIRST_SECOND_OF_2026 + 60, cost=3.0)
    history = await CostService(url).get_daily_history("2025-12-31", "2026-01-01")
    assert history == [
        {"date": "2025-12-31", "total": 1.0, "requests": 1},
        {"date": "2026-01-01", "total": 5.0, "requests": 2},
    ]


async def test_one_day_and_the_per_model_breakdown(url):
    await _log(url, at=_FIRST_SECOND_OF_2026, model="cheap", cost=0.5, tokens=(100, 50))
    await _log(url, at=_FIRST_SECOND_OF_2026, model="dear", cost=2.0, tokens=(10, 5))
    await _log(url, at=_FIRST_SECOND_OF_2026, model="dear", cost=1.0, tokens=(1, 1))
    await _log(url, at=_LAST_SECOND_OF_2025, model="dear", cost=9.0)  # the day before: excluded
    service = CostService(url)

    day = await service.get_daily_costs("2026-01-01")
    assert day == {"total": 3.5, "request_count": 3, "by_model": {"dear": 3.0, "cheap": 0.5}, "by_tool": {}}

    by_model = await service.get_costs_by_model("2026-01-01", "2026-01-01")
    assert by_model == [
        {"model": "dear", "total": 3.0, "requests": 2, "tokens": 17},
        {"model": "cheap", "total": 0.5, "requests": 1, "tokens": 150},
    ]


async def test_summary_since_a_timestamp(url):
    await _log(url, at=100.0, cost=1.0)
    await _log(url, at=200.0, cost=2.0)
    service = CostService(url)
    assert (await service.get_summary(since_timestamp=150.0)).total_usd == pytest.approx(2.0)
    assert (await service.get_summary()).total_usd == pytest.approx(3.0)


async def test_budget_status(url):
    service = CostService(url, daily_budget=10.0)
    await _log(url, at=time.time(), cost=8.0)
    status = await service.get_budget_status()
    assert status == {
        "daily_limit": 10.0,
        "spent_today": 8.0,
        "spent_pct": 80.0,
        "remaining": 2.0,
        "is_downgraded": True,
    }


def test_the_day_boundaries_used_above_are_what_they_claim():
    assert datetime.fromtimestamp(_LAST_SECOND_OF_2025, UTC).isoformat() == "2025-12-31T23:59:59+00:00"
    assert datetime.fromtimestamp(_FIRST_SECOND_OF_2026, UTC).isoformat() == "2026-01-01T00:00:00+00:00"
