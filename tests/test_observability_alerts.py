"""Alert rules, routing, and suppression (Sprint 9, T9.2).

The kill-tests from the acceptance criteria live here: forced stuck call, a dead
ElevenLabs key (as a TTS failure wave), and a full disk must each produce the
right alert at the right severity.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pincer.observability import alerts as alerts_mod
from pincer.observability.alerts import (
    Alert,
    Severity,
    deliver,
    disk_alert,
    doctor_alert,
    evaluate,
    scan,
)
from pincer.observability.golden_signals import GoldenSignals, Signal


@pytest.fixture(autouse=True)
def _reset():
    alerts_mod.reset_for_tests()
    yield
    alerts_mod.reset_for_tests()


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.ops_alerts_enabled = True
    cfg.ops_user_id = "ops-person"
    cfg.ops_channel = "telegram"
    cfg.ops_alert_email = ""
    cfg.ops_alert_repeat_min = 60
    cfg.alert_disk_free_min_pct = 10.0
    cfg.voice_max_call_duration = 600
    cfg.alert_stuck_call_grace_s = 60
    return cfg


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture ops-channel deliveries instead of sending them."""
    captured: list[tuple[str, str, str]] = []

    async def _notifier(user_id: str, channel: str, text: str) -> bool:
        captured.append((user_id, channel, text))
        return True

    from pincer.voice import status_notify

    status_notify.set_status_notifier(_notifier)
    yield captured
    status_notify.set_status_notifier(None)


def _signal(name: str, value, *, target=None, sample=100, min_sample=1, unit="ratio", detail=None) -> Signal:
    return Signal(
        name=name,
        value=value,
        unit=unit,
        sample_size=sample,
        min_sample=min_sample,
        target=target,
        window="1h",
        detail=detail or {},
    )


def _signals(**overrides) -> GoldenSignals:
    base = {
        "call_success_rate": _signal(
            "call_success_rate", 1.0, target=0.85, detail={"completed": 100, "terminated": 100, "by_failure_code": {}}
        ),
        "booking_success_rate": _signal(
            "booking_success_rate",
            1.0,
            target=0.70,
            detail={"confirmed": 10, "cooperative_attempts": 10, "by_result": {}},
        ),
        "turn_latency": _signal(
            "turn_latency_p95", 1.0, target=2.5, unit="s", detail={"p50_s": 0.8, "p95_s": 1.0, "slo_p95_s": 2.0}
        ),
        "stuck_calls": _signal(
            "stuck_calls", 0.0, target=0.0, unit="count", detail={"stuck": [], "threshold_seconds": 660}
        ),
        "cost_per_call": _signal("cost_per_call", 1.0, target=2.0, unit="ratio_to_baseline", detail={}),
    }
    base.update(overrides)
    return GoldenSignals(**base)


# ── Healthy system ───────────────────────────────────────────────────


def test_healthy_signals_fire_nothing(settings):
    assert evaluate(_signals(), settings) == []


def test_insufficient_data_never_fires(settings):
    """0% success on 1 call must not page anyone."""
    signals = _signals(
        call_success_rate=_signal(
            "call_success_rate",
            0.0,
            target=0.85,
            sample=1,
            min_sample=5,
            detail={"completed": 0, "terminated": 1, "by_failure_code": {"ws_drop": 1}},
        )
    )
    assert evaluate(signals, settings) == []


def test_missing_data_never_fires(settings):
    signals = _signals(call_success_rate=_signal("call_success_rate", None, target=0.85, sample=0, min_sample=5))
    assert evaluate(signals, settings) == []


# ── Kill-test 1: forced stuck call ───────────────────────────────────


def test_stuck_call_pages_immediately(settings):
    signals = _signals(
        stuck_calls=_signal(
            "stuck_calls",
            1.0,
            target=0.0,
            unit="count",
            detail={
                "threshold_seconds": 660,
                "stuck": [{"call_sid": "CA_stuck", "duration_seconds": 900, "over_by_seconds": 240}],
            },
        )
    )
    fired = evaluate(signals, settings)
    assert len(fired) == 1
    assert fired[0].rule == "stuck_calls"
    assert fired[0].severity is Severity.PAGE
    assert "CA_stuck" in fired[0].detail
    assert "runbook.md#stuck-call" in fired[0].runbook


def test_pages_sort_before_notifications(settings):
    signals = _signals(
        stuck_calls=_signal(
            "stuck_calls",
            1.0,
            target=0.0,
            unit="count",
            detail={
                "threshold_seconds": 660,
                "stuck": [{"call_sid": "CA", "duration_seconds": 900, "over_by_seconds": 240}],
            },
        ),
        call_success_rate=_signal(
            "call_success_rate",
            0.10,
            target=0.85,
            sample=50,
            detail={"completed": 5, "terminated": 50, "by_failure_code": {"ws_drop": 45}},
        ),
    )
    fired = evaluate(signals, settings)
    assert [a.severity for a in fired] == [Severity.PAGE, Severity.NOTIFY]


# ── Kill-test 2: dead ElevenLabs key → TTS failure wave ──────────────


def test_tts_failure_wave_notifies_with_the_dominant_code(settings):
    signals = _signals(
        call_success_rate=_signal(
            "call_success_rate",
            0.20,
            target=0.85,
            sample=25,
            detail={"completed": 5, "terminated": 25, "by_failure_code": {"tts_error": 18, "no_audio": 2, "none": 5}},
        )
    )
    fired = evaluate(signals, settings)
    assert len(fired) == 1
    assert fired[0].rule == "call_success_rate"
    # The alert names the dominant failure so the operator knows where to look.
    assert "tts_error×18" in fired[0].detail
    assert "runbook.md#call-success-rate-drop" in fired[0].runbook


# ── Kill-test 3: full disk ───────────────────────────────────────────


def test_full_disk_pages(settings, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: type("U", (), {"total": 100 * 10**9, "free": 2 * 10**9})())
    alert = disk_alert(settings)
    assert alert is not None
    assert alert.severity is Severity.PAGE
    assert "runbook.md#disk-full" in alert.runbook
    assert alert.value == pytest.approx(2.0)


def test_healthy_disk_is_silent(settings, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: type("U", (), {"total": 100 * 10**9, "free": 60 * 10**9})())
    assert disk_alert(settings) is None


def test_unreadable_disk_does_not_crash(settings, monkeypatch):
    import shutil

    def _boom(_p):
        raise OSError("no such volume")

    monkeypatch.setattr(shutil, "disk_usage", _boom)
    assert disk_alert(settings) is None


# ── Other rules ──────────────────────────────────────────────────────


def test_latency_alert_points_at_the_latency_report(settings):
    signals = _signals(
        turn_latency=_signal(
            "turn_latency_p95",
            3.4,
            target=2.5,
            unit="s",
            sample=40,
            detail={"p50_s": 1.2, "p95_s": 3.4, "slo_p95_s": 2.0},
        )
    )
    fired = evaluate(signals, settings)
    assert fired[0].rule == "turn_latency_p95"
    assert "latency-report" in fired[0].detail


def test_cost_alert_fires_on_the_ratio(settings):
    signals = _signals(
        cost_per_call=_signal(
            "cost_per_call",
            3.1,
            target=2.0,
            unit="ratio_to_baseline",
            sample=40,
            detail={"recent_p95_usd": 0.9, "baseline_p95_usd": 0.29, "calls_in_window": 12},
        )
    )
    fired = evaluate(signals, settings)
    assert fired[0].rule == "cost_per_call"


def test_booking_alert(settings):
    signals = _signals(
        booking_success_rate=_signal(
            "booking_success_rate",
            0.30,
            target=0.70,
            sample=10,
            detail={"confirmed": 3, "cooperative_attempts": 10, "by_result": {"declined": 7}},
        )
    )
    assert evaluate(signals, settings)[0].rule == "booking_success_rate"


def test_doctor_alert_pages_on_critical(settings, monkeypatch):
    from pincer.security.doctor import CheckResult, CheckStatus

    fake_report = MagicMock()
    fake_report.checks = [
        CheckResult(name="prod_auth_tokens", status=CheckStatus.CRITICAL, message="token missing"),
        CheckResult(name="ok_check", status=CheckStatus.PASS, message="fine"),
    ]
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: fake_report)

    alert = doctor_alert(settings)
    assert alert is not None
    assert alert.severity is Severity.PAGE
    assert "prod_auth_tokens" in alert.detail


# ── Delivery, suppression, recovery ──────────────────────────────────


async def test_alert_is_delivered_to_the_ops_channel(settings, sent):
    await deliver(settings, [Alert(rule="r", severity=Severity.NOTIFY, title="T", detail="D")])
    assert len(sent) == 1
    assert sent[0][0] == "ops-person"
    assert sent[0][1] == "telegram"
    assert "T" in sent[0][2]


async def test_repeat_notifications_are_suppressed(settings, sent):
    alert = Alert(rule="r", severity=Severity.NOTIFY, title="T", detail="D")
    await deliver(settings, [alert])
    await deliver(settings, [alert])
    assert len(sent) == 1


async def test_recovery_is_announced(settings, sent):
    alert = Alert(rule="r", severity=Severity.NOTIFY, title="T", detail="D")
    await deliver(settings, [alert])
    await deliver(settings, [])  # condition cleared
    assert len(sent) == 2
    assert "RESOLVED" in sent[1][2]


async def test_recovery_clears_the_suppression_window(settings, sent):
    """After a resolve, a re-fire must be announced, not swallowed."""
    alert = Alert(rule="r", severity=Severity.NOTIFY, title="T", detail="D")
    await deliver(settings, [alert])
    await deliver(settings, [])
    await deliver(settings, [alert])
    assert sum(1 for s in sent if "RESOLVED" not in s[2]) == 2


async def test_disabling_alerts_suppresses_delivery(settings, sent):
    settings.ops_alerts_enabled = False
    await deliver(settings, [Alert(rule="r", severity=Severity.PAGE, title="T", detail="D")])
    assert sent == []


async def test_undeliverable_alert_is_logged_loudly(settings, caplog):
    """A silent alerting system looks exactly like a healthy one."""
    from pincer.voice import status_notify

    status_notify.set_status_notifier(None)  # nothing wired
    settings.ops_user_id = ""
    settings.ops_alert_email = ""

    with caplog.at_level("ERROR"):
        await deliver(settings, [Alert(rule="r", severity=Severity.PAGE, title="T", detail="D")])
    assert any("OPS ALERT UNDELIVERED" in r.message for r in caplog.records)


async def test_scan_evaluates_and_delivers(settings, sent, monkeypatch):
    """End to end: signals -> rules -> ops channel."""
    signals = _signals(
        stuck_calls=_signal(
            "stuck_calls",
            2.0,
            target=0.0,
            unit="count",
            detail={
                "threshold_seconds": 660,
                "stuck": [
                    {"call_sid": "CA1", "duration_seconds": 900, "over_by_seconds": 240},
                    {"call_sid": "CA2", "duration_seconds": 950, "over_by_seconds": 290},
                ],
            },
        )
    )

    async def _collect(_settings, active_calls=None):
        return signals

    monkeypatch.setattr("pincer.observability.golden_signals.collect", _collect)
    fired = await scan(settings)
    assert [a.rule for a in fired] == ["stuck_calls"]
    assert len(sent) == 1
    assert "CA1" in sent[0][2]


async def test_alert_render_includes_the_runbook_link():
    alert = Alert(
        rule="r",
        severity=Severity.PAGE,
        title="T",
        detail="D",
        value=0.5,
        threshold=0.85,
        runbook="docs/operations/runbook.md#x",
    )
    rendered = alert.render()
    assert "🚨" in rendered
    assert "Runbook: docs/operations/runbook.md#x" in rendered
    assert "0.5" in rendered
