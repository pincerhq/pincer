"""Outbound abuse gate (Sprint 8, T8.3).

The acceptance criterion is bypass-resistance: quiet hours, the daily cap, the
target cooldown, and the do-not-call list must hold no matter which channel
initiates the dial — chat tool, dashboard REST, appointment scheduler, or an
automatic retry. Every one of those reaches Twilio through
`voice.outbound.make_phone_call`, so the tests drive that function and assert
Twilio was never called.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from pincer.voice import outbound
from pincer.voice.safety_gates import (
    BlockReason,
    add_do_not_call,
    calls_today,
    check_outbound_allowed,
    detect_opt_out,
    honor_opt_out,
    in_quiet_hours,
    is_do_not_call,
    list_do_not_call,
    normalize_number,
    parse_quiet_hours,
    record_outbound_call,
    remove_do_not_call,
)

TARGET = "+4915112345678"
OTHER = "+4915199999999"


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = True
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.voice_outbound_max_daily = 100  # per-user limit out of the way
    cfg.voice_max_call_duration = 600
    cfg.voice_machine_detection = False
    cfg.voice_engine = "conversation_relay"
    cfg.voice_default_language = "en"
    cfg.voice_supported_languages = "en,de"
    cfg.voice_language = "en-US"
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.voice_consent_mode = "two_party"
    cfg.voice_consent_language = ""
    cfg.voice_recording_enabled = False
    cfg.voice_assistant_name = "Pincer"
    cfg.voice_assistant_org = "3days.ai"
    cfg.voice_assistant_owner = ""
    cfg.voice_intro_text = ""
    cfg.voice_ws_auth_required = False
    cfg.twilio_account_sid = "AC123"
    cfg.twilio_auth_token.get_secret_value.return_value = "token"
    cfg.twilio_phone_number = "+4915212345678"
    # Sprint 8 gate settings
    cfg.voice_daily_call_limit = 20
    cfg.voice_target_cooldown_min = 60
    cfg.voice_quiet_hours = ""  # opened per-test; most tests are not about time
    cfg.voice_quiet_hours_override_users = ""
    cfg.voice_retry_attempts = 2
    return cfg


@pytest.fixture
def dialer(settings, monkeypatch):
    """`make_phone_call` wired to a fake Twilio client that records every dial."""
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    placed: list[dict[str, Any]] = []

    class FakeCalls:
        def create(self, **kwargs: Any) -> Any:
            placed.append(kwargs)
            call = MagicMock()
            call.sid = f"CA_{len(placed)}"
            return call

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = FakeCalls()

    twilio_rest = types.ModuleType("twilio.rest")
    twilio_rest.Client = FakeClient
    twilio_mod = types.ModuleType("twilio")
    twilio_mod.rest = twilio_rest
    monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
    monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)
    monkeypatch.setattr(outbound, "_daily_outbound_counts", {})

    async def call(number: str = TARGET, user_id: str = "u1", channel: str = "telegram", **kwargs: Any) -> str:
        return await outbound.make_phone_call(
            number, "confirm appointment", context={"user_id": user_id, "channel": channel}, **kwargs
        )

    call.placed = placed  # type: ignore[attr-defined]
    return call


# ── Do-not-call list ─────────────────────────────────────────────────


async def test_do_not_call_add_is_idempotent(settings):
    assert await add_do_not_call(settings, TARGET, reason="asked to stop") is True
    assert await add_do_not_call(settings, TARGET, reason="asked again") is False
    assert await is_do_not_call(settings, TARGET)
    entries = await list_do_not_call(settings)
    assert len(entries) == 1
    assert entries[0]["reason"] == "asked again"


async def test_do_not_call_matches_regardless_of_formatting(settings):
    await add_do_not_call(settings, "+49 151 123 456 78")
    assert await is_do_not_call(settings, "+4915112345678")
    assert await is_do_not_call(settings, "+49-151-123-456-78")


async def test_do_not_call_removal(settings):
    await add_do_not_call(settings, TARGET)
    assert await remove_do_not_call(settings, TARGET) is True
    assert await is_do_not_call(settings, TARGET) is False
    assert await remove_do_not_call(settings, TARGET) is False


async def test_do_not_call_blocks_the_dial(settings, dialer):
    await add_do_not_call(settings, TARGET, reason="callee opt-out")
    result = await dialer()
    assert result.startswith("Error")
    assert "do-not-call list" in result
    assert dialer.placed == []


async def test_do_not_call_is_honored_across_all_initiating_users(settings, dialer):
    """The list is global: user A's opt-out blocks user B's call too."""
    await add_do_not_call(settings, TARGET, source="callee", call_sid="CA_prev")
    for user in ("alice", "bob", "dashboard"):
        result = await dialer(user_id=user)
        assert "do-not-call list" in result
    assert dialer.placed == []


@pytest.mark.parametrize(
    "utterance",
    [
        "Please don't call me again.",
        "Stop calling me.",
        "Never call this number again",
        "Take me off your list",
        "Remove this number from your database",
        "Rufen Sie mich bitte nicht mehr an.",
        "Löschen Sie meine Nummer",
        "Nie wieder anrufen!",
        "Keine weiteren Anrufe bitte",
        "Більше не дзвоніть",
    ],
)
def test_opt_out_intent_detected_in_both_languages(utterance):
    assert detect_opt_out(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "Yes, Tuesday at three works.",
        "Can you call me back tomorrow?",
        "Rufen Sie mich morgen noch mal an.",
        "I'll call you later.",
        "",
    ],
)
def test_opt_out_not_triggered_by_ordinary_speech(utterance):
    """A false positive silently blacklists a number the user needs."""
    assert not detect_opt_out(utterance)


async def test_honor_opt_out_adds_the_callee(settings):
    added = await honor_opt_out(settings, TARGET, "Please don't call me again.", call_sid="CA_1")
    assert added
    assert await is_do_not_call(settings, TARGET)
    entry = (await list_do_not_call(settings))[0]
    assert entry["source"] == "callee"
    assert entry["call_sid"] == "CA_1"


async def test_honor_opt_out_ignores_a_normal_call(settings):
    assert await honor_opt_out(settings, TARGET, "Tuesday at three is fine, thanks.") is False
    assert await is_do_not_call(settings, TARGET) is False


# ── Quiet hours ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20:00-08:00", (1200, 480)),
        ("9:30-17:00", (570, 1020)),
        ("", None),
        ("nonsense", None),
        ("25:00-08:00", None),
        ("08:00-08:00", None),  # zero-length window = off
    ],
)
def test_parse_quiet_hours(value, expected):
    assert parse_quiet_hours(value) == expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(21, True), (23, True), (2, True), (7, True), (8, False), (12, False), (19, False), (20, True)],
)
def test_quiet_hours_window_wraps_midnight(settings, hour, expected):
    settings.voice_quiet_hours = "20:00-08:00"
    now = datetime(2026, 8, 20, hour, 30, tzinfo=UTC)
    assert in_quiet_hours(settings, now) is expected


async def test_quiet_hours_block_the_dial(settings, dialer, monkeypatch):
    settings.voice_quiet_hours = "20:00-08:00"
    monkeypatch.setattr("pincer.voice.safety_gates.in_quiet_hours", lambda cfg, now=None: True)
    result = await dialer()
    assert "quiet hours" in result
    assert dialer.placed == []


async def test_quiet_hours_message_is_localized(settings, monkeypatch):
    settings.voice_quiet_hours = "20:00-08:00"
    monkeypatch.setattr("pincer.voice.safety_gates.in_quiet_hours", lambda cfg, now=None: True)
    decision = await check_outbound_allowed(settings, TARGET, user_id="u1", language="de")
    assert decision.reason == BlockReason.QUIET_HOURS
    assert "Ruhezeit" in decision.message


async def test_quiet_hours_override_user_may_still_call(settings, dialer, monkeypatch):
    settings.voice_quiet_hours = "20:00-08:00"
    settings.voice_quiet_hours_override_users = "oncall,ops"
    monkeypatch.setattr("pincer.voice.safety_gates.in_quiet_hours", lambda cfg, now=None: True)
    assert (await check_outbound_allowed(settings, TARGET, user_id="oncall")).allowed
    assert not (await check_outbound_allowed(settings, TARGET, user_id="someone_else")).allowed


# ── Global daily cap ─────────────────────────────────────────────────


async def test_daily_cap_counts_every_user_and_channel(settings, dialer):
    settings.voice_daily_call_limit = 3
    settings.voice_target_cooldown_min = 0  # isolate the cap from the cooldown

    assert not (await dialer(number="+4915100000001", user_id="alice")).startswith("Error")
    assert not (await dialer(number="+4915100000002", user_id="bob", channel="web")).startswith("Error")
    assert not (await dialer(number="+4915100000003", user_id="dashboard", channel="web")).startswith("Error")
    assert await calls_today(settings) == 3

    blocked = await dialer(number="+4915100000004", user_id="carol")
    assert "Daily outbound call limit" in blocked
    assert len(dialer.placed) == 3


async def test_daily_cap_of_zero_means_no_global_cap(settings):
    settings.voice_daily_call_limit = 0
    settings.voice_target_cooldown_min = 0
    for i in range(30):
        await record_outbound_call(settings, f"+491511234{i:04d}")
    assert (await check_outbound_allowed(settings, TARGET)).allowed


# ── Per-target cooldown ──────────────────────────────────────────────


async def test_target_cooldown_bounds_calls_to_one_number(settings, dialer):
    """The cooldown budgets the first call plus its configured retries; the
    call after that is hammering and is refused."""
    settings.voice_retry_attempts = 2  # → 3 attempts allowed in the window

    for _ in range(3):
        assert not (await dialer()).startswith("Error")

    blocked = await dialer()
    assert "already been called" in blocked
    assert len(dialer.placed) == 3


async def test_cooldown_is_per_number(settings, dialer):
    settings.voice_retry_attempts = 0  # → 1 attempt per number per window
    assert not (await dialer(number=TARGET)).startswith("Error")
    assert (await dialer(number=TARGET)).startswith("Error")
    assert not (await dialer(number=OTHER)).startswith("Error")


async def test_cooldown_reports_a_retry_after(settings):
    settings.voice_retry_attempts = 0
    settings.voice_target_cooldown_min = 60
    await record_outbound_call(settings, TARGET)
    decision = await check_outbound_allowed(settings, TARGET)
    assert decision.reason == BlockReason.TARGET_COOLDOWN
    assert 1 <= decision.retry_after_min <= 61


async def test_cooldown_of_zero_disables_the_check(settings):
    settings.voice_target_cooldown_min = 0
    for _ in range(10):
        await record_outbound_call(settings, TARGET)
    assert (await check_outbound_allowed(settings, TARGET)).allowed


# ── Bypass resistance ────────────────────────────────────────────────


async def test_retries_share_the_daily_cap(settings, dialer):
    """A retry storm is impossible: redials consume the same budget as a
    user-initiated call because both go through make_phone_call."""
    settings.voice_daily_call_limit = 2
    settings.voice_target_cooldown_min = 0

    from pincer.voice.scheduling import AppointmentTask, _place_call

    task = AppointmentTask(
        task_id="t1",
        user_id="u1",
        channel="telegram",
        target_number=TARGET,
        contact_name="Dr. Schmidt",
        topic="checkup",
        timeframe="this week",
        duration_minutes=30,
        language="en",
        candidates=[],
    )
    assert not (await _place_call(task, settings)).startswith("Error")
    assert not (await _place_call(task, settings)).startswith("Error")
    third = await _place_call(task, settings)
    assert "Daily outbound call limit" in third
    assert len(dialer.placed) == 2


async def test_dashboard_api_hits_the_same_gate(settings, dialer, monkeypatch):
    """T8.2: POST /api/voice/calls must not be a way around the limits."""
    from fastapi import HTTPException

    from pincer.api.voice import InitiateCallIn as Body
    from pincer.api.voice import initiate_call

    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    await add_do_not_call(settings, TARGET, reason="opt-out")

    with pytest.raises(HTTPException) as exc:
        await initiate_call(Body(target_number=TARGET, purpose="confirm appointment"))
    assert exc.value.status_code == 403
    assert dialer.placed == []


async def test_gate_precedence_do_not_call_wins_over_everything(settings, monkeypatch):
    """A blocked number stays blocked even when every other limit is open."""
    settings.voice_daily_call_limit = 0
    settings.voice_target_cooldown_min = 0
    settings.voice_quiet_hours = ""
    await add_do_not_call(settings, TARGET)
    decision = await check_outbound_allowed(settings, TARGET)
    assert decision.reason == BlockReason.DO_NOT_CALL


async def test_allowed_call_is_recorded_for_the_next_decision(settings, dialer):
    settings.voice_target_cooldown_min = 60
    settings.voice_retry_attempts = 0
    await dialer()
    assert await calls_today(settings) == 1
    assert not (await check_outbound_allowed(settings, TARGET)).allowed


def test_normalize_number_strips_formatting():
    assert normalize_number("+49 (151) 123-456-78") == "+4915112345678"
    assert normalize_number("4915112345678") == "+4915112345678"
    assert normalize_number("") == ""
