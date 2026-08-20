"""Golden signals and the failure taxonomy (Sprint 9, T9.1/T9.3).

The signals decide when someone gets woken up, so the tests care most about the
two ways a monitoring system fails people: firing on noise, and staying quiet
when it shouldn't.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from pincer.observability.failure_codes import (
    EXCLUDED_FROM_SLO,
    FailureCode,
    classify_failure,
    counts_against_slo,
    describe,
)
from pincer.observability.golden_signals import (
    booking_success_rate,
    call_attempt_success_rate,
    call_success_rate,
    collect,
    cost_per_call,
    percentile,
    stuck_calls,
    turn_latency,
)
from pincer.voice.retention import VOICE_TABLES_SQL, ensure_voice_tables


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.voice_max_call_duration = 600
    cfg.alert_stuck_call_grace_s = 60
    cfg.alert_call_success_window_h = 2
    cfg.alert_call_success_min_volume = 5
    cfg.alert_call_success_min = 0.85
    cfg.alert_booking_window_h = 24
    cfg.alert_booking_min_volume = 3
    cfg.alert_booking_success_min = 0.70
    cfg.alert_latency_window_h = 1
    cfg.alert_latency_min_turns = 10
    cfg.alert_latency_p95_max_s = 2.5
    cfg.slo_latency_p95_s = 2.0
    cfg.slo_call_attempt_success = 0.99
    cfg.alert_cost_baseline_days = 7
    cfg.alert_cost_p95_multiplier = 2.0
    cfg.alert_cost_min_calls = 10
    return cfg


async def _seed_calls(settings, codes: list[str], hours_ago: float = 0.5) -> None:
    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    ended = (datetime.now(UTC) - timedelta(hours=hours_ago) + timedelta(seconds=60)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        for i, code in enumerate(codes):
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code) "
                "VALUES (?, 'outbound', ?, ?, ?)",
                (f"CA{i}_{code}", started, ended, code),
            )
        await db.commit()


def _write_turns(settings, totals_ms: list[float], hours_ago: float = 0.1) -> None:
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for i, total in enumerate(totals_ms):
            fh.write(json.dumps({"ts": stamp, "call_sid": f"CA{i}", "turn": i, "total_ms": total}) + "\n")


# ── Failure taxonomy ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("timeout_ringing", FailureCode.NO_ANSWER),
        ("no-answer", FailureCode.NO_ANSWER),
        ("busy", FailureCode.BUSY),
        ("voicemail detected", FailureCode.VOICEMAIL),
        ("repeated_errors", FailureCode.LLM_ERROR),
        ("shutdown drain", FailureCode.SHUTDOWN),
        ("stuck_max_duration_exceeded", FailureCode.STUCK),
        ("websocket closed 1006", FailureCode.WS_DROP),
        ("Twilio error 64111 converting tokens to speech", FailureCode.TTS_ERROR),
        ("64101 invalid twiml url", FailureCode.TWIML_ERROR),
        ("do-not-call list", FailureCode.DO_NOT_CALL),
        ("quiet_hours", FailureCode.QUIET_HOURS),
        ("timeout_greeting", FailureCode.SILENT_CALLEE),
        ("timeout_freeform", FailureCode.PHASE_TIMEOUT),
        ("call_end_cleanup", FailureCode.CALLEE_HANGUP),
        ("something we have never seen", FailureCode.UNKNOWN),
        ("", FailureCode.UNKNOWN),
    ],
)
def test_classify_failure(reason, expected):
    assert classify_failure(reason, completed=False) is expected


def test_completed_call_has_no_failure_code():
    assert classify_failure("anything at all", completed=True) is FailureCode.NONE


def test_specific_patterns_win_over_generic_ones():
    """`timeout_ringing` must reach NO_ANSWER, not the generic PHASE_TIMEOUT."""
    assert classify_failure("timeout_ringing") is FailureCode.NO_ANSWER
    assert classify_failure("timeout_verify") is FailureCode.PHASE_TIMEOUT


@pytest.mark.parametrize("code", sorted(EXCLUDED_FROM_SLO))
def test_callee_and_policy_outcomes_do_not_burn_budget(code):
    assert not counts_against_slo(code)


@pytest.mark.parametrize("code", [FailureCode.WS_DROP, FailureCode.TTS_ERROR, FailureCode.LLM_ERROR, FailureCode.STUCK])
def test_our_own_failures_burn_budget(code):
    assert counts_against_slo(code)


def test_unknown_code_is_assumed_ours():
    """A code we cannot parse must count against us, not be silently forgiven."""
    assert counts_against_slo("something_new_from_the_future")


def test_every_code_has_a_description():
    for code in FailureCode:
        assert describe(code) and "Unclassified" not in describe(code)


# ── Call success rate ────────────────────────────────────────────────


async def test_call_success_rate_counts_completed_over_terminated(settings):
    await _seed_calls(settings, ["none", "none", "none", "no_answer", "ws_drop", "none"])
    signal = await call_success_rate(settings)
    assert signal.value == pytest.approx(4 / 6)
    assert signal.sample_size == 6
    assert signal.detail["by_failure_code"]["ws_drop"] == 1


async def test_call_success_rate_needs_a_minimum_sample(settings):
    """One failed call out of one is not an 85% violation."""
    await _seed_calls(settings, ["ws_drop"])
    signal = await call_success_rate(settings)
    assert signal.value == 0.0
    assert not signal.sufficient_data  # so no rule can fire on it


async def test_call_success_rate_ignores_calls_outside_the_window(settings):
    await _seed_calls(settings, ["none"] * 5, hours_ago=0.5)
    await _seed_calls(settings, ["ws_drop"] * 5, hours_ago=48)
    signal = await call_success_rate(settings)
    assert signal.sample_size == 5
    assert signal.value == 1.0


async def test_call_success_rate_with_no_tables_is_none_not_zero(settings):
    """No data must never render as 0% — that would page on an empty system."""
    signal = await call_success_rate(settings)
    assert signal.value is None
    assert not signal.sufficient_data


async def test_attempt_success_excludes_unreachable_and_policy(settings):
    await _seed_calls(
        settings,
        ["none", "none", "no_answer", "voicemail", "do_not_call", "quiet_hours", "ws_drop"],
    )
    signal = await call_attempt_success_rate(settings, window_hours=24)
    # Eligible: 2 completed + 1 ws_drop = 3
    assert signal.sample_size == 3
    assert signal.value == pytest.approx(2 / 3)
    assert signal.detail["excluded_callee_or_policy"] == 4


# ── Booking success rate ─────────────────────────────────────────────


async def _seed_bookings(settings, results: list[str]) -> None:
    from pincer.observability.bookings import record_booking_outcome

    for i, result in enumerate(results):
        await record_booking_outcome(settings, task_id=f"task{i}", result=result, language="de")


async def test_booking_rate_excludes_unreachable_from_the_denominator(settings):
    await _seed_bookings(settings, ["confirmed", "confirmed", "declined", "unreachable", "unreachable"])
    signal = await booking_success_rate(settings)
    assert signal.sample_size == 3  # unreachable removed
    assert signal.value == pytest.approx(2 / 3)


async def test_booking_outcome_is_keyed_by_task_not_call(settings):
    """A task retried three times is ONE booking attempt, not three."""
    from pincer.observability.bookings import record_booking_outcome

    for attempt in (1, 2, 3):
        await record_booking_outcome(
            settings, task_id="task-retry", result="unreachable", call_sid=f"CA{attempt}", attempts=attempt
        )
    signal = await booking_success_rate(settings)
    assert signal.detail["by_result"] == {"unreachable": 1}


async def test_calendar_failure_is_not_a_confirmed_booking(settings):
    """Agreed on the call but the calendar write failed is our bug, not a win."""
    await _seed_bookings(settings, ["confirmed", "calendar_failed", "calendar_failed", "declined"])
    signal = await booking_success_rate(settings)
    assert signal.value == pytest.approx(1 / 4)


# ── Turn latency ─────────────────────────────────────────────────────


async def test_turn_latency_p95(settings):
    _write_turns(settings, [500.0] * 90 + [4000.0] * 10)
    signal = await turn_latency(settings)
    assert signal.sample_size == 100
    assert signal.value == pytest.approx(4.0, abs=0.5)
    assert signal.detail["p50_s"] == pytest.approx(0.5)


async def test_turn_latency_needs_enough_turns(settings):
    _write_turns(settings, [9000.0, 9000.0])
    signal = await turn_latency(settings)
    assert not signal.sufficient_data


async def test_turn_latency_ignores_old_records(settings):
    _write_turns(settings, [500.0] * 12, hours_ago=0.1)
    _write_turns(settings, [9000.0] * 12, hours_ago=48)
    signal = await turn_latency(settings)
    assert signal.sample_size == 12
    assert signal.value == pytest.approx(0.5)


def test_percentile_interpolates():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert percentile([], 0.95) is None
    assert percentile([7.0], 0.95) == 7.0


# ── Stuck calls ──────────────────────────────────────────────────────


def _call_state(duration: int) -> MagicMock:
    state = MagicMock()
    state.duration_seconds = duration
    state.direction = "outbound"
    return state


def test_stuck_calls_detects_calls_past_the_limit(settings):
    active = {"CA_ok": _call_state(120), "CA_stuck": _call_state(700)}
    signal = stuck_calls(settings, active)
    assert signal.value == 1.0
    assert signal.detail["stuck"][0]["call_sid"] == "CA_stuck"
    assert signal.detail["stuck"][0]["over_by_seconds"] == 40


def test_stuck_calls_respects_the_grace_period(settings):
    """max_duration + grace, not max_duration — Twilio hangs up a hair late."""
    assert stuck_calls(settings, {"CA": _call_state(650)}).value == 0.0
    assert stuck_calls(settings, {"CA": _call_state(661)}).value == 1.0


def test_no_active_calls_is_not_stuck(settings):
    assert stuck_calls(settings, {}).value == 0.0


# ── Cost per call ────────────────────────────────────────────────────


async def _seed_costs(settings, totals: list[float], hours_ago: float = 1.0) -> None:
    from pincer.observability.call_costs import ensure_call_costs_table

    recorded = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_call_costs_table(db)
        for i, total in enumerate(totals):
            await db.execute(
                "INSERT OR REPLACE INTO call_costs (call_sid, total_usd, recorded_at) VALUES (?, ?, ?)",
                (f"CA_cost_{hours_ago}_{i}", total, recorded),
            )
        await db.commit()


async def test_cost_per_call_is_a_ratio_to_the_baseline(settings):
    await _seed_costs(settings, [0.10] * 20, hours_ago=100)  # prior week
    await _seed_costs(settings, [0.30] * 5, hours_ago=1)  # today, 3x
    signal = await cost_per_call(settings)
    assert signal.value == pytest.approx(3.0, abs=0.1)
    assert signal.sufficient_data


async def test_cost_baseline_excludes_the_window_being_judged(settings):
    """A spike must not be allowed to raise its own baseline.

    With overlapping windows the expensive calls appear in both p95s, the ratio
    collapses toward 1.0, and the alert silently never fires.
    """
    await _seed_costs(settings, [0.10] * 15, hours_ago=100)
    await _seed_costs(settings, [1.00] * 15, hours_ago=1)  # today: 10x, and the majority of the week
    signal = await cost_per_call(settings)
    assert signal.detail["baseline_p95_usd"] == pytest.approx(0.10)
    assert signal.value == pytest.approx(10.0, abs=0.1)


async def test_cost_per_call_without_a_baseline_is_none(settings):
    await _seed_costs(settings, [0.30] * 3, hours_ago=1)
    signal = await cost_per_call(settings)
    assert not signal.sufficient_data


# ── collect() ────────────────────────────────────────────────────────


async def test_collect_returns_all_five_signals(settings):
    signals = await collect(settings, active_calls={})
    names = {s.name for s in signals.all()}
    assert names == {
        "call_success_rate",
        "booking_success_rate",
        "turn_latency_p95",
        "stuck_calls",
        "cost_per_call",
        "busy_capacity",  # Sprint 12 §10.3
    }
    payload = signals.to_dict()
    assert payload["generated_at"]
    assert set(payload["signals"]) == names


async def test_collect_on_an_empty_system_is_all_insufficient(settings):
    """A brand-new install must produce zero alerts, not five."""
    from pincer.observability.alerts import evaluate

    signals = await collect(settings, active_calls={})
    assert evaluate(signals, settings) == []


def test_voice_tables_sql_has_the_sprint9_columns():
    for column in ("failure_code", "engine", "language", "report_delivered_at"):
        assert column in VOICE_TABLES_SQL
