"""SLOs and the error budget (Sprint 9, T9.5).

The freeze rule is an organisational commitment, so these tests care mostly
about it not firing for silly reasons and not being silently escapable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from pincer.observability.slo import (
    MIN_BUDGET_SAMPLE,
    availability_slo,
    call_success_slo,
    collect,
    latency_slo,
    report_delivery_slo,
)
from pincer.voice.retention import ensure_voice_tables


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.slo_call_attempt_success = 0.99
    cfg.slo_latency_p95_s = 2.0
    cfg.slo_report_delivery_s = 30.0
    cfg.slo_availability = 0.995
    cfg.slo_error_budget_freeze_pct = 50.0
    return cfg


async def _seed_calls(settings, codes: list[str], *, delivered_after_s: float | None = None) -> None:
    started = datetime.now(UTC) - timedelta(hours=1)
    ended = started + timedelta(seconds=60)
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        for i, code in enumerate(codes):
            delivered = (
                (ended + timedelta(seconds=delivered_after_s)).isoformat() if delivered_after_s is not None else None
            )
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code, "
                "report_delivered_at) VALUES (?, 'outbound', ?, ?, ?, ?)",
                (f"CA{i}_{code}", started.isoformat(), ended.isoformat(), code, delivered),
            )
        await db.commit()


def _write_turns(settings, totals_ms: list[float]) -> None:
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for i, total in enumerate(totals_ms):
            fh.write(json.dumps({"ts": stamp, "call_sid": f"CA{i % 20}", "turn": i, "total_ms": total}) + "\n")


async def _seed_canary(settings, results: list[bool]) -> None:
    from pincer.observability.canary import CANARY_TABLE_SQL

    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(CANARY_TABLE_SQL)
        for i, ok in enumerate(results):
            await db.execute(
                "INSERT INTO canary_runs (ran_at, ok, skipped) VALUES (?, ?, 0)",
                ((datetime.now(UTC) - timedelta(hours=i)).isoformat(), int(ok)),
            )
        await db.commit()


# ── Call attempt success ─────────────────────────────────────────────


async def test_call_slo_excludes_unreachable_from_the_budget(settings):
    """A week of voicemails must not burn budget nobody can defend."""
    await _seed_calls(settings, ["none"] * 99 + ["ws_drop"] + ["no_answer"] * 50)
    status = await call_success_slo(settings)
    assert status.sample_size == 100  # the 50 no_answer are out
    assert status.actual == pytest.approx(0.99)
    assert status.met is True


async def test_call_slo_burn_is_failures_over_budget(settings):
    await _seed_calls(settings, ["none"] * 98 + ["ws_drop", "tts_error"])
    status = await call_success_slo(settings)
    # Budget = 100 attempts x 1% = 1.0; spent 2 -> 200% burned.
    assert status.budget_total == pytest.approx(1.0)
    assert status.budget_spent == 2.0
    assert status.burn_pct == pytest.approx(200.0)
    assert status.met is False


async def test_call_slo_with_no_data_is_none_not_perfect(settings):
    status = await call_success_slo(settings)
    assert status.actual is None
    assert status.met is None


# ── Latency ──────────────────────────────────────────────────────────


async def test_latency_slo_budget_is_five_percent_of_turns(settings):
    _write_turns(settings, [500.0] * 95 + [3000.0] * 5)
    status = await latency_slo(settings)
    assert status.sample_size == 100
    assert status.budget_total == pytest.approx(5.0)
    assert status.budget_spent == 5.0
    assert status.burn_pct == pytest.approx(100.0)


async def test_latency_slo_met_when_p95_under_target(settings):
    _write_turns(settings, [900.0] * 100)
    status = await latency_slo(settings)
    assert status.actual == pytest.approx(0.9)
    assert status.met is True


async def test_latency_slo_with_no_turns(settings):
    status = await latency_slo(settings)
    assert status.actual is None
    assert status.sample_size == 0


# ── Report delivery ──────────────────────────────────────────────────


async def test_report_delivery_measures_hangup_to_delivery(settings):
    await _seed_calls(settings, ["none"] * 10, delivered_after_s=5.0)
    status = await report_delivery_slo(settings)
    assert status.sample_size == 10
    assert status.actual == pytest.approx(5.0)
    assert status.met is True


async def test_slow_reports_burn_budget(settings):
    await _seed_calls(settings, ["none"] * 10, delivered_after_s=45.0)
    status = await report_delivery_slo(settings)
    assert status.met is False
    assert status.budget_spent == 10.0


async def test_undelivered_reports_are_not_counted_as_fast(settings):
    """Only successful deliveries are stamped; unstamped rows are excluded."""
    await _seed_calls(settings, ["none"] * 5, delivered_after_s=None)
    status = await report_delivery_slo(settings)
    assert status.sample_size == 0
    assert status.actual is None


# ── Availability ─────────────────────────────────────────────────────


async def test_availability_is_derived_from_canary_runs(settings):
    await _seed_canary(settings, [True] * 19 + [False])
    status = await availability_slo(settings)
    assert status.actual == pytest.approx(0.95)
    assert status.met is False


async def test_availability_is_labelled_inferred(settings):
    """We only know the service was up when the canary ran — never claim more."""
    await _seed_canary(settings, [True] * 5)
    assert (await availability_slo(settings)).confidence == "inferred"


# ── Freeze rule ──────────────────────────────────────────────────────


async def test_no_freeze_on_an_empty_system(settings):
    report = await collect(settings)
    assert report["feature_freeze"] is False
    assert report["freeze_reason"] == ""


async def test_no_freeze_below_the_minimum_sample(settings):
    """Three slow turns on a quiet Tuesday must not freeze the roadmap."""
    _write_turns(settings, [500.0] * 5 + [9000.0] * 5)
    report = await collect(settings)
    latency = next(s for s in report["slos"] if s["name"] == "turn_latency_p95")
    assert latency["burn_pct"] > 50.0  # the budget IS burned
    assert report["feature_freeze"] is False  # but the sample is too small to act on


async def test_freeze_fires_with_enough_data(settings):
    _write_turns(settings, [500.0] * (MIN_BUDGET_SAMPLE - 20) + [9000.0] * 20)
    report = await collect(settings)
    assert report["feature_freeze"] is True
    assert "turn_latency_p95" in report["freeze_reason"]


async def test_freeze_threshold_is_configurable(settings):
    settings.slo_error_budget_freeze_pct = 5000.0
    _write_turns(settings, [500.0] * (MIN_BUDGET_SAMPLE - 20) + [9000.0] * 20)
    report = await collect(settings)
    assert report["feature_freeze"] is False


async def test_collect_reports_all_four_slos(settings):
    report = await collect(settings)
    assert {s["name"] for s in report["slos"]} == {
        "call_attempt_success",
        "turn_latency_p95",
        "report_delivery",
        "availability",
    }
    assert report["freeze_min_sample"] == MIN_BUDGET_SAMPLE
