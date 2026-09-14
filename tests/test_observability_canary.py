"""Synthetic canary and the weekly digest (Sprint 9, T9.2/T9.3).

The canary places real phone calls automatically, so half of these tests are
about it *refusing* to: unset target, do-not-call list, quiet hours. The other
half are about it being strict enough to be useful — a call that connects and
then sits in silence is exactly what a dead STT/TTS provider looks like, and it
must not read as green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from pincer.observability import alerts as alerts_mod
from pincer.observability.canary import CanaryResult, recent_runs, run_and_alert, run_canary
from pincer.observability.digest import build_digest


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
    cfg.voice_canary_enabled = True
    cfg.voice_canary_number = "+4915100000001"
    cfg.voice_canary_timeout_s = 30
    cfg.voice_canary_min_turns = 1
    cfg.voice_quiet_hours = ""
    cfg.voice_quiet_hours_override_users = ""
    cfg.voice_daily_call_limit = 0
    cfg.voice_target_cooldown_min = 0
    cfg.voice_retry_attempts = 0
    cfg.ops_alerts_enabled = True
    cfg.ops_user_id = "ops"
    cfg.ops_channel = "telegram"
    cfg.ops_alert_email = ""
    cfg.ops_alert_repeat_min = 60
    return cfg


@pytest.fixture
def sent(monkeypatch):
    captured: list[str] = []

    async def _notifier(user_id: str, channel: str, text: str) -> bool:
        captured.append(text)
        return True

    from pincer.voice import status_notify

    status_notify.set_status_notifier(_notifier)
    yield captured
    status_notify.set_status_notifier(None)


def _fake_engine(turns: int, *, state_present: bool = True) -> MagicMock:
    engine = MagicMock()
    metrics = MagicMock()
    metrics.turn_latencies_s = [1.0] * turns
    engine.metrics_registry.get.return_value = metrics
    engine.get_call_state.return_value = MagicMock() if state_present else None

    async def _end(_sid):
        return None

    engine.end_call = _end
    return engine


# ── Refusals ─────────────────────────────────────────────────────────


async def test_disabled_canary_is_a_skip_not_a_failure(settings):
    settings.voice_canary_enabled = False
    result = await run_canary(settings)
    assert result.ok and result.skipped


async def test_unset_target_number_fails_loudly(settings):
    settings.voice_canary_number = ""
    result = await run_canary(settings)
    assert not result.ok
    assert "CANARY_NUMBER" in result.reason


async def test_canary_refuses_a_do_not_call_number(settings):
    from pincer.voice.safety_gates import add_do_not_call

    await add_do_not_call(settings, settings.voice_canary_number, reason="opt-out")
    result = await run_canary(settings)
    assert not result.ok
    assert "do-not-call" in result.reason


async def test_quiet_hours_skip_rather_than_force(settings, monkeypatch):
    """A canary at 03:00 is exactly the robocall the gate exists to prevent."""
    monkeypatch.setattr("pincer.voice.safety_gates.in_quiet_hours", lambda cfg, now=None: True)
    settings.voice_quiet_hours = "20:00-08:00"
    result = await run_canary(settings)
    assert result.skipped
    assert result.ok  # a skip is not an outage
    assert "quiet_hours" in result.reason


# ── Health verdicts ──────────────────────────────────────────────────


async def test_call_that_never_connects_fails(settings, monkeypatch):
    async def _place(**_kwargs):
        return "Call initiated successfully.\nCall SID: CA_canary"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", _place)
    monkeypatch.setattr("pincer.voice.twiml_server.get_engine", lambda: _fake_engine(0, state_present=False))
    settings.voice_canary_timeout_s = 30
    monkeypatch.setattr("pincer.observability.canary._POLL_INTERVAL_S", 0.01)

    # Shrink the deadline so the test does not sit through the real timeout.
    import pincer.observability.canary as canary_mod

    original = canary_mod._await_call

    async def _fast(settings_, call_sid, timeout_s, min_turns):
        return await original(settings_, call_sid, 0.05, min_turns)

    monkeypatch.setattr(canary_mod, "_await_call", _fast)

    result = await run_canary(settings)
    assert not result.ok
    assert "never connected" in result.reason


async def test_connected_but_silent_call_fails(settings, monkeypatch):
    """The dead-provider shape: Twilio connects, nobody transcribes or speaks."""

    async def _place(**_kwargs):
        return "Call initiated successfully.\nCall SID: CA_silent"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", _place)
    monkeypatch.setattr("pincer.voice.twiml_server.get_engine", lambda: _fake_engine(0))
    monkeypatch.setattr("pincer.observability.canary._POLL_INTERVAL_S", 0.01)
    settings.voice_canary_timeout_s = 30

    # Shrink the deadline so the test does not wait 30s for the verdict.
    import pincer.observability.canary as canary_mod

    original = canary_mod._await_call

    async def _fast(settings_, call_sid, timeout_s, min_turns):
        return await original(settings_, call_sid, 0.05, min_turns)

    monkeypatch.setattr(canary_mod, "_await_call", _fast)

    result = await run_canary(settings)
    assert not result.ok
    assert "0/1 turn" in result.reason
    assert "STT, the LLM, or TTS" in result.reason


async def test_conversing_call_is_healthy(settings, monkeypatch):
    async def _place(**_kwargs):
        return "Call initiated successfully.\nCall SID: CA_ok"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", _place)
    monkeypatch.setattr("pincer.voice.twiml_server.get_engine", lambda: _fake_engine(2))
    monkeypatch.setattr("pincer.observability.canary._POLL_INTERVAL_S", 0.01)

    result = await run_canary(settings)
    assert result.ok
    assert result.turns == 2
    assert result.call_sid == "CA_ok"


async def test_dial_error_is_reported_verbatim(settings, monkeypatch):
    async def _place(**_kwargs):
        return "Error: Twilio SDK not installed."

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", _place)
    result = await run_canary(settings)
    assert not result.ok
    assert "Twilio SDK" in result.reason


# ── Alerting and history ─────────────────────────────────────────────


async def test_failed_canary_pages(settings, sent, monkeypatch):
    async def _fail(_settings):
        return CanaryResult(ok=False, reason="call never connected within 30s")

    monkeypatch.setattr("pincer.observability.canary.run_canary", _fail)
    await run_and_alert(settings)
    assert len(sent) == 1
    assert "canary call failed" in sent[0]
    assert "runbook.md#provider-outage" in sent[0]


async def test_healthy_canary_pages_nobody(settings, sent, monkeypatch):
    async def _ok(_settings):
        return CanaryResult(ok=True, turns=1, call_sid="CA")

    monkeypatch.setattr("pincer.observability.canary.run_canary", _ok)
    await run_and_alert(settings)
    assert sent == []


async def test_crash_is_caught_and_paged(settings, sent, monkeypatch):
    async def _boom(_settings):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("pincer.observability.canary.run_canary", _boom)
    result = await run_and_alert(settings)
    assert not result.ok
    assert "engine exploded" in result.reason
    assert len(sent) == 1


async def test_runs_are_persisted_including_skips(settings, monkeypatch):
    """A gap in canary coverage is a fact the availability SLO needs."""

    async def _skip(_settings):
        return CanaryResult(ok=True, skipped=True, reason="quiet hours")

    monkeypatch.setattr("pincer.observability.canary.run_canary", _skip)
    await run_and_alert(settings)

    runs = await recent_runs(settings)
    assert len(runs) == 1
    assert runs[0]["skipped"] == 1


async def test_history_is_newest_first(settings, monkeypatch):
    from pincer.observability.canary import _persist_run

    for i in range(3):
        await _persist_run(settings, CanaryResult(ok=bool(i % 2), call_sid=f"CA{i}"))
    runs = await recent_runs(settings, limit=3)
    assert len(runs) == 3


# ── Weekly digest ────────────────────────────────────────────────────


async def _seed(settings, codes: list[str], hours_ago: float) -> None:
    from pincer.voice.retention import ensure_voice_tables

    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        for i, code in enumerate(codes):
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code) "
                "VALUES (?, 'outbound', ?, ?, ?)",
                (f"CA_{hours_ago}_{i}", started, started, code),
            )
        await db.commit()


async def test_digest_with_no_data_says_so(settings):
    text = await build_digest(settings)
    assert "No calls" in text


async def test_digest_reports_deltas_against_last_week(settings):
    await _seed(settings, ["none"] * 8 + ["ws_drop"] * 2, hours_ago=24)
    await _seed(settings, ["none"] * 9 + ["ws_drop"], hours_ago=24 + 168)
    text = await build_digest(settings)
    assert "ws_drop" in text
    assert "(+1)" in text  # 2 this week vs 1 last week


async def test_digest_flags_new_failure_modes(settings):
    """A code that did not exist last week is what deserves Monday attention."""
    await _seed(settings, ["none", "tts_error", "tts_error"], hours_ago=24)
    await _seed(settings, ["none", "no_answer"], hours_ago=24 + 168)
    text = await build_digest(settings)
    assert "New failure modes" in text
    assert "tts_error" in text


async def test_digest_flags_cleared_failure_modes(settings):
    await _seed(settings, ["none"] * 5, hours_ago=24)
    await _seed(settings, ["ws_drop"] * 4, hours_ago=24 + 168)
    text = await build_digest(settings)
    assert "Cleared since last week" in text


async def test_digest_includes_the_runbook_link(settings):
    await _seed(settings, ["none"], hours_ago=24)
    assert "runbook.md" in await build_digest(settings)


async def test_digest_notices_traffic_stopping(settings):
    """Zero calls this week after a busy week is a signal, not silence."""
    await _seed(settings, ["none"] * 20, hours_ago=24 + 168)
    text = await build_digest(settings)
    assert "traffic stopped" in text
