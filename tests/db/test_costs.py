"""Cost accounting on the repository/service layer, on SQLite and Postgres."""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pincer.config.pricing import PriceTable, Rates, TokenUsage, load_price_table
from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.exceptions import BudgetExceededError
from pincer.models.costs import CostLog, ImageCostLog
from pincer.repositories.costs import CostLogRepository
from pincer.services.costs import CostService

_TABLES = [CostLog.__table__, ImageCostLog.__table__]
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Test rates, USD per million tokens: never the shipped pincer.toml.
_PRICES = PriceTable({"gpt-4o": Rates(input=2.0, cache_write=2.0, cache_write_1h=2.0, cache_read=0.5, output=8.0)})

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


async def _log(
    url: str,
    *,
    at: float,
    model: str = "gpt-4o",
    cost: float = 1.0,
    tokens: tuple[int, int] = (10, 5),
    cache: tuple[int, int, int] = (0, 0, 0),
):
    """`cache`: (read, 5-minute write, 1-hour write) tokens."""
    async with session_scope(url) as session:
        await CostLogRepository(session).add(
            CostLog(
                timestamp=at,
                provider="test",
                model=model,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                cache_read_tokens=cache[0],
                cache_write_5m_tokens=cache[1],
                cache_write_1h_tokens=cache[2],
                cost_usd=cost,
            )
        )


async def test_record_prices_the_call_from_the_price_table_and_stores_it(url):
    service = CostService(url, prices=_PRICES)
    cost = await service.record("openai", "gpt-4o", TokenUsage(input_tokens=1000, output_tokens=500), session_id="s1")
    assert cost == pytest.approx((1000 * 2.0 + 500 * 8.0) / 1_000_000)
    summary = await service.get_summary()
    assert (summary.total_calls, summary.total_input_tokens, summary.total_output_tokens) == (1, 1000, 500)
    assert summary.total_usd == pytest.approx(cost)


async def test_a_free_provider_costs_nothing(url):
    assert (
        await CostService(url, prices=_PRICES).record(
            "local", "gpt-4o", TokenUsage(input_tokens=1000, output_tokens=500), is_free=True
        )
        == 0.0
    )


async def test_going_over_budget_raises_and_records_nothing(url):
    service = CostService(url, daily_budget=0.001, prices=_PRICES)
    with pytest.raises(BudgetExceededError):
        await service.record("openai", "gpt-4o", TokenUsage(input_tokens=1_000_000, output_tokens=0))  # $2.00
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
    await _log(url, at=_FIRST_SECOND_OF_2026, model="cheap", cost=0.5, tokens=(100, 50), cache=(800, 0, 0))
    await _log(url, at=_FIRST_SECOND_OF_2026, model="dear", cost=2.0, tokens=(10, 5), cache=(1_000, 200, 30))
    await _log(url, at=_FIRST_SECOND_OF_2026, model="dear", cost=1.0, tokens=(1, 1), cache=(2_000, 0, 4))
    await _log(url, at=_LAST_SECOND_OF_2025, model="dear", cost=9.0, cache=(5, 5, 5))  # the day before: excluded
    service = CostService(url)

    day = await service.get_daily_costs("2026-01-01")
    assert day == {
        "total": 3.5,
        "request_count": 3,
        "by_model": {"dear": 3.0, "cheap": 0.5},
        "by_tool": {},
        "cache_read_tokens": 3_800,
        "cache_write_5m_tokens": 200,
        "cache_write_1h_tokens": 34,
    }

    by_model = await service.get_costs_by_model("2026-01-01", "2026-01-01")
    # `tokens` stays uncached input + output; the cache counts sit beside it
    assert by_model == [
        {
            "model": "dear",
            "total": 3.0,
            "requests": 2,
            "tokens": 17,
            "input_tokens": 11,
            "cache_read_tokens": 3_000,
            "cache_write_5m_tokens": 200,
            "cache_write_1h_tokens": 34,
        },
        {
            "model": "cheap",
            "total": 0.5,
            "requests": 1,
            "tokens": 150,
            "input_tokens": 100,
            "cache_read_tokens": 800,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
        },
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


async def test_an_unpriced_model_raises_logs_and_stores_nothing(url, caplog):
    import logging

    from pincer.config.pricing import UnpricedModelError

    service = CostService(url, prices=_PRICES)
    with caplog.at_level(logging.ERROR, logger="pincer.services.costs"), pytest.raises(UnpricedModelError):
        await service.record("openai", "gpt-4o-2024-08-06", TokenUsage(input_tokens=1000, output_tokens=500))
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "pincer.services.costs"] == [
        ("ERROR", "No [pricing] entry for model 'gpt-4o-2024-08-06' (provider openai): the call is not recorded")
    ]
    assert (await service.get_summary()).total_calls == 0


async def test_a_provider_multiplier_prices_only_that_providers_calls(url, tmp_path):
    (tmp_path / "pincer.toml").write_text(
        '[pricing."claude-sonnet-5-5"]\n'
        "input = 2.0\ncache_write = 2.5\ncache_write_1h = 4.0\ncache_read = 0.1\noutput = 10.0\n\n"
        '[pricing.provider."foundry"]\nmultiplier = 1.1\n',
        encoding="utf-8",
    )
    service = CostService(url, prices=load_price_table(tmp_path))
    plain = await service.record("anthropic", "claude-sonnet-5-5", TokenUsage(input_tokens=10_000, output_tokens=1_000))
    zoned = await service.record("foundry", "claude-sonnet-5-5", TokenUsage(input_tokens=10_000, output_tokens=1_000))
    assert plain == pytest.approx(0.03, abs=1e-12)
    assert zoned == pytest.approx(0.033, abs=1e-12)
    assert (await service.get_summary()).total_usd == pytest.approx(0.063, abs=1e-12)  # what was stored


def _shipped(tmp_path: Path) -> PriceTable:
    """The repository's pincer.toml alone: never the developer's pincer.local.toml."""
    shutil.copy(_REPO_ROOT / "pincer.toml", tmp_path / "pincer.toml")
    return load_price_table(tmp_path)


async def test_every_token_type_is_priced_and_stored(url, tmp_path):
    """The benchmark oracle (Sonnet 5.5, run B): 36 uncached, 27,559 5-minute writes,
    281,470 reads, 1,226 output at 2 / 2.50 / 0.10 / 10 per MTok = $0.1093765."""
    service = CostService(url, prices=_shipped(tmp_path))
    usage = TokenUsage(input_tokens=36, cache_write_5m_tokens=27_559, cache_read_tokens=281_470, output_tokens=1_226)
    cost = await service.record("anthropic", "claude-sonnet-5-5", usage, session_id="s1")
    assert cost == pytest.approx(0.1093765, abs=1e-12)
    async with session_scope(url) as session:
        (row,) = await CostLogRepository(session).list()
    assert (
        row.input_tokens,
        row.cache_write_5m_tokens,
        row.cache_write_1h_tokens,
        row.cache_read_tokens,
        row.output_tokens,
        row.cost_usd,
    ) == (36, 27_559, 0, 281_470, 1_226, pytest.approx(0.1093765, abs=1e-12))
