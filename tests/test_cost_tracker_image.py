"""Tests for image cost tracking in CostTracker."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pincer.llm.cost_tracker import CostTracker


@pytest.fixture
async def tracker(tmp_path: Path):
    ct = CostTracker(tmp_path / "test.db", daily_budget=10.0)
    await ct.initialize()
    yield ct
    await ct.close()


@pytest.mark.asyncio
async def test_add_image_cost_recorded(tracker: CostTracker):
    await tracker.add_image_cost(0.003, "fal", "fal-ai/nano-banana-2")
    count = await tracker.get_image_count_today()
    assert count == 1


@pytest.mark.asyncio
async def test_image_cost_included_in_today_spend(tracker: CostTracker):
    await tracker.add_image_cost(0.005, "gemini", "gemini-2.5-flash-image")
    spend = await tracker.get_today_spend()
    assert spend == pytest.approx(0.005, abs=1e-7)


@pytest.mark.asyncio
async def test_combined_llm_and_image_spend(tracker: CostTracker):
    await tracker.record("anthropic", "claude-haiku-4-5-20251001", 1000, 500)
    await tracker.add_image_cost(0.003, "fal")
    spend = await tracker.get_today_spend()
    assert spend > 0.003


@pytest.mark.asyncio
async def test_image_count_today_multiple(tracker: CostTracker):
    await tracker.add_image_cost(0.003, "fal")
    await tracker.add_image_cost(0.003, "fal")
    await tracker.add_image_cost(0.004, "gemini")
    count = await tracker.get_image_count_today()
    assert count == 3
