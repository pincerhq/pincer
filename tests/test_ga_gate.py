"""GA gate evaluation (Sprint 10, T10.4).

The gate decides whether a product ships, so the tests care most about the one
way it could do harm: reading "no data" as "fine".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from pincer.observability.call_costs import ensure_call_costs_table
from pincer.observability.ga_gate import (
    GAThresholds,
    Verdict,
    booking_success,
    call_volume_and_success,
    compliance_incidents,
    cost_per_call,
    evaluate,
    latency_per_language,
    render_markdown,
    security_findings,
)
from pincer.voice.retention import ensure_voice_tables


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.voice_consent_mode = "two_party"
    cfg.voice_quiet_hours = "20:00-08:00"
    cfg.voice_transcript_retention_days = 90
    return cfg


@pytest.fixture
def thresholds() -> GAThresholds:
    """Small thresholds so tests exercise the logic, not seed 200 rows."""
    return GAThresholds(
        min_calls=10,
        min_bookings=4,
        min_priced_calls=5,
        min_turns_per_language=5,
    )


async def _seed_calls(settings, codes: list[str], days_ago: float = 1.0) -> None:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        for i, code in enumerate(codes):
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code, language) "
                "VALUES (?, 'outbound', ?, ?, ?, 'de')",
                (f"CA{days_ago}_{i}", started.isoformat(), (started + timedelta(seconds=60)).isoformat(), code),
            )
        await db.commit()


async def _seed_costs(settings, totals: list[float]) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_call_costs_table(db)
        for i, total in enumerate(totals):
            await db.execute(
                "INSERT INTO call_costs (call_sid, total_usd, twilio_usd, llm_usd, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (f"CAcost{i}", total, total * 0.6, total * 0.4, datetime.now(UTC).isoformat()),
            )
        await db.commit()


def _write_turns(settings, per_language: dict[str, list[float]]) -> None:
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        counter = 0
        for language, totals in per_language.items():
            for total in totals:
                counter += 1
                fh.write(
                    json.dumps(
                        {"ts": stamp, "call_sid": f"CA{counter}", "turn": 1, "total_ms": total, "language": language}
                    )
                    + "\n"
                )


# ── "No data" is never a pass ────────────────────────────────────────


async def test_empty_system_is_not_ready(settings):
    report = await evaluate(settings, days=14, thresholds=GAThresholds())
    assert not report.ready
    assert all(c.verdict is not Verdict.PASS for c in report.criteria if c.key == "call_volume")


async def test_zero_calls_reports_insufficient_not_pass(settings, thresholds):
    criteria = await call_volume_and_success(settings, 14, thresholds)
    volume = next(c for c in criteria if c.key == "call_volume")
    assert volume.verdict is Verdict.INSUFFICIENT
    assert "10 more real calls" in volume.needed


async def test_perfect_but_tiny_sample_is_insufficient(settings, thresholds):
    """Three flawless calls is not a 95% success rate."""
    await _seed_calls(settings, ["none", "none", "none"])
    criteria = await call_volume_and_success(settings, 14, thresholds)
    success = next(c for c in criteria if c.key == "call_success_rate")
    assert success.verdict is Verdict.INSUFFICIENT
    assert success.evidence["rate"] == 1.0  # the number is right, the sample is not


async def test_ready_requires_every_criterion(settings, thresholds, monkeypatch):
    """One undecided criterion is enough to block, even with nothing failing."""
    report = await evaluate(settings, days=14, thresholds=thresholds)
    assert not report.ready
    assert not report.failed or report.blocked


# ── Call volume and success ──────────────────────────────────────────


async def test_success_excludes_callee_unreachable(settings, thresholds):
    await _seed_calls(settings, ["none"] * 10 + ["no_answer"] * 20 + ["voicemail"] * 10)
    criteria = await call_volume_and_success(settings, 14, thresholds)
    success = next(c for c in criteria if c.key == "call_success_rate")
    assert success.evidence["eligible"] == 10
    assert success.evidence["excluded_callee_or_policy"] == 30
    assert success.verdict is Verdict.PASS


async def test_success_fails_when_measured_and_missed(settings, thresholds):
    await _seed_calls(settings, ["none"] * 8 + ["ws_drop"] * 4)
    criteria = await call_volume_and_success(settings, 14, thresholds)
    success = next(c for c in criteria if c.key == "call_success_rate")
    assert success.verdict is Verdict.FAIL
    # Evidence is rounded to 4 dp for the report; compare with that tolerance.
    assert success.evidence["rate"] == pytest.approx(8 / 12, abs=1e-4)


async def test_one_stuck_call_fails_the_criterion(settings, thresholds):
    """Zero means zero — a percentage would let one through."""
    await _seed_calls(settings, ["none"] * 20 + ["stuck"])
    criteria = await call_volume_and_success(settings, 14, thresholds)
    stuck = next(c for c in criteria if c.key == "zero_stuck_calls")
    assert stuck.verdict is Verdict.FAIL
    assert stuck.evidence["stuck"] == 1


async def test_calls_outside_the_window_do_not_count(settings, thresholds):
    await _seed_calls(settings, ["none"] * 20, days_ago=90)
    criteria = await call_volume_and_success(settings, 14, thresholds)
    assert next(c for c in criteria if c.key == "call_volume").evidence["calls"] == 0


# ── Booking ──────────────────────────────────────────────────────────


async def _seed_bookings(settings, results: list[str]) -> None:
    from pincer.observability.bookings import record_booking_outcome

    for i, result in enumerate(results):
        await record_booking_outcome(settings, task_id=f"t{i}", result=result)


async def test_booking_passes_both_bars(settings, thresholds):
    await _seed_bookings(settings, ["confirmed"] * 9 + ["declined"] + ["unreachable"] * 2)
    criterion = await booking_success(settings, 14, thresholds)
    assert criterion.verdict is Verdict.PASS
    assert criterion.evidence["cooperative_rate"] == pytest.approx(0.9)


async def test_booking_fails_when_only_one_bar_is_met(settings, thresholds):
    """90% of cooperative callees, but voicemails drag the overall below 70%."""
    await _seed_bookings(settings, ["confirmed"] * 9 + ["declined"] + ["unreachable"] * 10)
    criterion = await booking_success(settings, 14, thresholds)
    assert criterion.evidence["cooperative_rate"] == pytest.approx(0.9)
    assert criterion.evidence["overall_rate"] == pytest.approx(9 / 20)
    assert criterion.verdict is Verdict.FAIL


async def test_calendar_failures_are_not_confirmations(settings, thresholds):
    await _seed_bookings(settings, ["confirmed"] * 5 + ["calendar_failed"] * 5)
    criterion = await booking_success(settings, 14, thresholds)
    assert criterion.evidence["cooperative_rate"] == pytest.approx(0.5)
    assert criterion.verdict is Verdict.FAIL


# ── Latency, per language ────────────────────────────────────────────


def test_latency_passes_when_every_language_meets_the_bar(settings, thresholds):
    _write_turns(settings, {"en": [800.0] * 10, "de": [900.0] * 10})
    criterion = latency_per_language(settings, 14, thresholds)
    assert criterion.verdict is Verdict.PASS


def test_latency_fails_when_one_language_misses(settings, thresholds):
    """English fine, German slow — an aggregate would have hidden this."""
    _write_turns(settings, {"en": [700.0] * 10, "de": [3000.0] * 10})
    criterion = latency_per_language(settings, 14, thresholds)
    assert criterion.verdict is Verdict.FAIL
    assert "de" in criterion.summary
    assert criterion.evidence["de"]["p95_s"] == pytest.approx(3.0)


def test_latency_insufficient_when_a_language_is_missing(settings, thresholds):
    _write_turns(settings, {"en": [800.0] * 10})
    criterion = latency_per_language(settings, 14, thresholds)
    assert criterion.verdict is Verdict.INSUFFICIENT
    assert "de" in criterion.summary


def test_latency_reports_other_languages_without_gating(settings, thresholds):
    _write_turns(settings, {"en": [800.0] * 10, "de": [800.0] * 10, "uk": [9000.0] * 3})
    criterion = latency_per_language(settings, 14, thresholds)
    assert criterion.verdict is Verdict.PASS
    assert criterion.evidence["other_languages"]["uk"]["turns"] == 3


# ── Cost ─────────────────────────────────────────────────────────────


async def test_cost_within_model_passes(settings, thresholds):
    await _seed_costs(settings, [0.18] * 10)
    criterion = await cost_per_call(settings, 14, thresholds)
    assert criterion.verdict is Verdict.PASS
    assert criterion.evidence["mean_usd"] == pytest.approx(0.18)


async def test_cost_below_model_passes_but_is_reported(settings, thresholds):
    """Cheaper than planned is good news that still has to reach pricing."""
    await _seed_costs(settings, [0.05] * 10)
    criterion = await cost_per_call(settings, 14, thresholds)
    assert criterion.verdict is Verdict.PASS
    assert "below model" in criterion.summary


async def test_cost_above_model_fails(settings, thresholds):
    await _seed_costs(settings, [0.60] * 10)
    criterion = await cost_per_call(settings, 14, thresholds)
    assert criterion.verdict is Verdict.FAIL
    assert "ABOVE model" in criterion.summary


async def test_cost_warns_about_price_accuracy(settings, thresholds):
    await _seed_costs(settings, [0.18] * 10)
    criterion = await cost_per_call(settings, 14, thresholds)
    assert "PINCER_PRICE_" in criterion.evidence["note"]


# ── Security and compliance ──────────────────────────────────────────


def test_security_fails_on_critical(settings, monkeypatch):
    from pincer.security.doctor import CheckResult, CheckStatus

    report = MagicMock()
    report.checks = [CheckResult(name="prod_auth_tokens", status=CheckStatus.CRITICAL, message="missing")]
    report.score = 40
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: report)

    criterion = security_findings(settings)
    assert criterion.verdict is Verdict.FAIL
    assert criterion.evidence["critical"][0]["name"] == "prod_auth_tokens"


def test_security_passes_with_warnings_only(settings, monkeypatch):
    from pincer.security.doctor import CheckResult, CheckStatus

    report = MagicMock()
    report.checks = [CheckResult(name="voice_canary", status=CheckStatus.WARNING, message="off")]
    report.score = 90
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: report)

    assert security_findings(settings).verdict is Verdict.PASS


async def test_compliance_passes_with_guardrails_active(settings):
    criterion = await compliance_incidents(settings, 14)
    assert criterion.verdict is Verdict.PASS


async def test_compliance_fails_on_weakened_consent(settings):
    settings.voice_consent_mode = "one_party"
    criterion = await compliance_incidents(settings, 14)
    assert criterion.verdict is Verdict.FAIL
    assert "two_party" in criterion.summary


async def test_compliance_fails_on_a_do_not_call_violation(settings):
    """A call that reached a blocked number means the gate was bypassed."""
    from pincer.voice.safety_gates import add_do_not_call, record_outbound_call

    await add_do_not_call(settings, "+4915100000001", reason="opt-out")
    await record_outbound_call(settings, "+4915100000001", user_id="u1")

    criterion = await compliance_incidents(settings, 14)
    assert criterion.verdict is Verdict.FAIL
    assert "bypassed" in criterion.summary


async def test_blocked_dials_are_evidence_of_health_not_failure(settings):
    """The gate refusing calls is the system working."""
    criterion = await compliance_incidents(settings, 14)
    assert criterion.verdict is Verdict.PASS
    assert "blocked_dials" in criterion.evidence


# ── Manual criteria ──────────────────────────────────────────────────


async def test_manual_criteria_never_auto_pass(settings, thresholds):
    report = await evaluate(settings, days=14, thresholds=thresholds)
    manual = [c for c in report.criteria if c.verdict is Verdict.MANUAL]
    assert {c.key for c in manual} == {"alert_quality", "pilot_feedback"}
    for criterion in manual:
        assert criterion.needed, f"{criterion.key} does not say what a human must bring"


# ── Report rendering ─────────────────────────────────────────────────


async def test_markdown_report_states_the_verdict(settings, thresholds):
    report = await evaluate(settings, days=14, thresholds=thresholds)
    markdown = render_markdown(report)
    assert "# GA Gate Review" in markdown
    assert "NOT READY" in markdown
    assert "Decision required" in markdown
    for criterion in report.criteria:
        assert criterion.title in markdown


async def test_markdown_lists_undecided_in_the_decision_table(settings, thresholds):
    report = await evaluate(settings, days=14, thresholds=thresholds)
    markdown = render_markdown(report)
    table_section = markdown.split("## Decision required")[1]
    for criterion in report.failed + report.blocked:
        assert criterion.title in table_section


def test_report_to_dict_is_json_serialisable(settings):
    from pincer.observability.ga_gate import Criterion, GAGateReport

    report = GAGateReport(
        generated_at="2026-08-20T00:00:00Z",
        window_days=14,
        thresholds={},
        criteria=[Criterion(key="k", title="t", verdict=Verdict.PASS, summary="s")],
    )
    assert report.ready
    json.dumps(report.to_dict(), default=str)
