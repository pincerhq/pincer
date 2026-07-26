"""Tests for the predictor cache/lock lifecycle. No real torch/doctr model is built."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from doctr_mcp.predictors import PredictorParams, get_predictor, run_predictor


@pytest.mark.asyncio
async def test_same_params_share_cached_predictor(fake_predictor_builder: list[Any]) -> None:
    params = PredictorParams(task="ocr", det_arch="db_resnet50")
    p1 = await get_predictor(params)
    p2 = await get_predictor(params)
    assert p1 is p2
    assert len(fake_predictor_builder) == 1


@pytest.mark.asyncio
async def test_different_thresholds_build_different_predictors(fake_predictor_builder: list[Any]) -> None:
    """docTR mutates bin_thresh/box_thresh as instance attributes post-construction —
    the cache key must include them or two different-threshold calls would race on
    one shared predictor."""
    p1 = await get_predictor(PredictorParams(task="ocr", bin_thresh=0.1))
    p2 = await get_predictor(PredictorParams(task="ocr", bin_thresh=0.5))
    assert p1 is not p2
    assert len(fake_predictor_builder) == 2


@pytest.mark.asyncio
async def test_different_architectures_build_different_predictors(fake_predictor_builder: list[Any]) -> None:
    p1 = await get_predictor(PredictorParams(task="detection", det_arch="db_resnet50"))
    p2 = await get_predictor(PredictorParams(task="detection", det_arch="fast_base"))
    assert p1 is not p2


@pytest.mark.asyncio
async def test_lru_eviction_bounds_cache_size(
    monkeypatch: pytest.MonkeyPatch, fake_predictor_builder: list[Any]
) -> None:
    from doctr_mcp import predictors
    from doctr_mcp.config import get_settings

    monkeypatch.setenv("DOCTR_MAX_CACHED_PREDICTORS", "2")
    get_settings.cache_clear()

    p1 = await get_predictor(PredictorParams(task="ocr", det_arch="a1"))
    await get_predictor(PredictorParams(task="ocr", det_arch="a2"))
    await get_predictor(PredictorParams(task="ocr", det_arch="a3"))  # evicts a1

    assert len(predictors._get_cache()) == 2  # noqa: SLF001

    # a1 was evicted, so requesting it again must rebuild (4th build call).
    p1_again = await get_predictor(PredictorParams(task="ocr", det_arch="a1"))
    assert p1_again is not p1
    assert len(fake_predictor_builder) == 4


@pytest.mark.asyncio
async def test_concurrent_calls_for_same_key_build_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp import predictors

    build_count = 0

    def _sync_wrapper(params: object) -> str:
        # get_predictor calls predictor_builder via asyncio.to_thread, so it must be
        # a plain sync callable; simulate slowness with a thread-safe sleep instead.
        nonlocal build_count
        build_count += 1
        import time

        time.sleep(0.05)
        return "predictor"

    monkeypatch.setattr(predictors, "predictor_builder", _sync_wrapper)

    params = PredictorParams(task="ocr")
    results = await asyncio.gather(*(get_predictor(params) for _ in range(5)))

    assert build_count == 1
    assert all(r == "predictor" for r in results)


@pytest.mark.asyncio
async def test_run_predictor_respects_max_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp.config import get_settings

    monkeypatch.setenv("DOCTR_MAX_WORKERS", "1")
    get_settings.cache_clear()
    # Force a fresh semaphore for the new setting.
    import doctr_mcp.predictors as predictors_module

    predictors_module._inference_semaphore = None  # noqa: SLF001

    concurrent = 0
    max_concurrent = 0

    def _predictor(pages: list[int]) -> list[int]:
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        import time

        time.sleep(0.05)
        concurrent -= 1
        return pages

    await asyncio.gather(*(run_predictor(_predictor, [i]) for i in range(4)))

    assert max_concurrent == 1
