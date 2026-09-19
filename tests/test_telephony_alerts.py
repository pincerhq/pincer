"""
Alert rules: thresholds, windows, and the minimum sample size that stops one
bad call from paging anyone.

The rule that matters most is the last one: an alert must never fire on a
sample too small to mean anything, and it must say *why* it is not firing so
"quiet" and "blind" stay distinguishable.
"""

from __future__ import annotations

import asyncio

import pytest

from pincer.db import ensure_schema_current
from pincer.voice.telemetry import alerts as telephony_alerts
from pincer.voice.telemetry import runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.tracer import drain_pending

MS = 1_000_000


class _Settings:
    alert_response_latency_p95_ms = 500.0
    alert_response_latency_window_min = 60
    alert_telephony_window_min = 60
    alert_telephony_min_calls = 2
    alert_connection_rate_min = 0.90
    alert_technical_failure_rate_max = 0.05
    alert_unexpected_disconnect_rate_max = 0.02
    alert_stage_timeout_max = 1
    alert_audio_queue_p95_ms = 100.0
    alert_telemetry_coverage_min = 0.95
    telephony_min_samples = 3


async def _seed(db_path, *, latencies=(), failures=(), timeouts=0):
    runtime.reset_for_tests(db_path=str(db_path))
    await get_recorder().start()

    for index, latency_ms in enumerate(latencies):
        tracer = runtime.start_call(provider_call_id=f"CA_{index}", direction="inbound", engine="media_streams")
        tracer.answered()
        origin = tracer.watch.started_ns
        turn = tracer.start_turn(speech_end_ns=origin)
        turn.first_audio(kind="audio", at_ns=origin + int(latency_ms) * MS)
        turn.finish()
        for _ in range(timeouts):
            tracer.timeout("tool", limit_s=10.0, tool="calendar")
        await tracer.finish(status="completed", failure_code="none")

    for index, code in enumerate(failures):
        tracer = runtime.start_call(provider_call_id=f"CA_f{index}", direction="inbound", engine="media_streams")
        tracer.answered()
        await tracer.finish(status="failed", failure_code=code)

    await drain_pending()
    await get_recorder().flush()
    await get_recorder().stop(drain_timeout_s=1.0)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "telephony.db"
    ensure_schema_current(path)
    yield path
    asyncio.run(runtime.shutdown())
    runtime.reset_for_tests()


def _rule(rows, name):
    return next(row for row in rows if row.rule == name)


def test_latency_alert_fires_only_above_the_threshold(db):
    asyncio.run(_seed(db, latencies=[900, 950, 1000, 1100]))
    rows = asyncio.run(telephony_alerts.evaluate(db, _Settings()))
    latency = _rule(rows, "response_latency_p95")
    assert latency.firing is True
    assert latency.value is not None and latency.value > 500.0
    assert latency.samples == 4
    assert latency.evidence  # the calls that caused it


def test_latency_alert_stays_quiet_when_fast(db):
    asyncio.run(_seed(db, latencies=[100, 120, 90, 110]))
    latency = _rule(asyncio.run(telephony_alerts.evaluate(db, _Settings())), "response_latency_p95")
    assert latency.firing is False


def test_one_bad_call_out_of_one_does_not_page_anyone(db):
    """The minimum sample size is the whole point of this rule."""
    asyncio.run(_seed(db, latencies=[5000]))
    latency = _rule(asyncio.run(telephony_alerts.evaluate(db, _Settings())), "response_latency_p95")
    assert latency.firing is False
    assert latency.insufficient_data is True
    assert latency.reason == "insufficient turns in window"
    # The value is still reported — quiet is not the same as blind.
    assert latency.value is not None


def test_technical_failures_fire_but_callee_unavailability_does_not(db):
    asyncio.run(_seed(db, failures=["ws_drop", "ws_drop", "no_answer", "busy"]))
    rows = asyncio.run(telephony_alerts.evaluate(db, _Settings()))
    technical = _rule(rows, "technical_failure_rate")
    assert technical.firing is True
    assert technical.value == pytest.approx(0.5)

    # Same window, same calls: the disconnect rule counts only the ws_drops.
    disconnects = _rule(rows, "unexpected_disconnect_rate")
    assert disconnects.value == pytest.approx(0.5)


def test_policy_declines_never_reach_the_failure_rate(db):
    asyncio.run(_seed(db, failures=["blocked", "quiet_hours", "none", "none"]))
    technical = _rule(asyncio.run(telephony_alerts.evaluate(db, _Settings())), "technical_failure_rate")
    assert technical.value == pytest.approx(0.0)
    assert technical.firing is False


def test_tool_timeouts_are_counted_not_rated(db):
    asyncio.run(_seed(db, latencies=[100, 120], timeouts=2))
    tool = _rule(asyncio.run(telephony_alerts.evaluate(db, _Settings())), "tool_timeouts")
    assert tool.value == 4  # two calls × two timeouts
    assert tool.firing is True


def test_every_alert_links_to_a_filtered_dashboard(db):
    asyncio.run(_seed(db, latencies=[900, 950, 1000]))
    for alert in asyncio.run(telephony_alerts.evaluate(db, _Settings())):
        if alert.rule == "telemetry_export":
            continue  # not a windowed query
        assert alert.dashboard_filter, alert.rule


def test_telemetry_export_failure_is_itself_an_alert(db):
    """Telemetry that stopped arriving looks exactly like a healthy system."""
    asyncio.run(_seed(db, latencies=[100]))
    recorder = get_recorder()
    recorder.stats().dropped_queue_full = 5
    export = _rule(asyncio.run(telephony_alerts.evaluate(db, _Settings())), "telemetry_export")
    assert export.firing is True
    assert "lost" in export.detail


def test_thresholds_and_windows_are_configurable(db):
    asyncio.run(_seed(db, latencies=[900, 950, 1000, 1100]))

    class _Relaxed(_Settings):
        alert_response_latency_p95_ms = 5000.0

    latency = _rule(asyncio.run(telephony_alerts.evaluate(db, _Relaxed())), "response_latency_p95")
    assert latency.firing is False
    assert latency.threshold == 5000.0


def test_an_empty_window_fires_nothing(db):
    rows = asyncio.run(telephony_alerts.evaluate(db, _Settings()))
    firing = [row for row in rows if row.firing and row.rule != "telemetry_export"]
    assert firing == []
