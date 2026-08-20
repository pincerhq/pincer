"""End-to-end call instrumentation (Sprint 9).

Drives real calls through the harness and asserts the observability chain a
real incident depends on: the call ends → a failure code is classified → it is
persisted on `voice_calls` → a cost row exists → the golden signals see it.

Each link is separately testable, but only the chain is what an operator uses,
and every one of these links has been silently broken at least once in some
codebase.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import aiosqlite
import pytest
from voice_harness.personas import (
    CooperativePersona,
    HangsUpMidCallPersona,
    HostilePersona,
    SilentPersona,
    VoicemailPersona,
)
from voice_harness.runner import Scenario, run_scenario

from pincer.observability import call_costs
from pincer.observability.failure_codes import FailureCode
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.state_machine import CallPhase
from pincer.voice.transcript import Speaker, TranscriptLogger


@pytest.fixture(autouse=True)
def _reset():
    call_costs.reset_for_tests()
    yield
    call_costs.reset_for_tests()


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.voice_max_call_duration = 600
    cfg.alert_stuck_call_grace_s = 60
    cfg.price_twilio_outbound_per_min = 0.10
    cfg.price_twilio_inbound_per_min = 0.01
    cfg.price_conversationrelay_per_min = 0.06
    cfg.price_deepgram_per_min = 0.006
    cfg.price_elevenlabs_per_1k_chars = 0.05
    return cfg


def _channel(settings) -> object:
    from pincer.channels.phone_calls import VoiceChannel

    return VoiceChannel(settings)


# ── Failure classification at call end ───────────────────────────────


def test_completed_call_classifies_as_none(settings):
    channel = _channel(settings)
    sm = MagicMock()
    sm.state.transitions = []
    assert channel._classify_call_failure(sm, None, completed=True) is FailureCode.NONE


def test_failed_call_uses_the_state_machine_reason(settings):
    from pincer.voice.state_machine import CallStateMachine

    channel = _channel(settings)
    sm = CallStateMachine("CA1", is_outbound=True)
    sm.start_call()
    sm.force_terminal(CallPhase.FAILED, reason="timeout_ringing")
    assert channel._classify_call_failure(sm, None, completed=False) is FailureCode.NO_ANSWER


def test_silent_agent_is_no_audio_even_when_completed(settings):
    """Hotfix-3 shape: transcripts look healthy, the callee heard nothing.

    A 'completed' call whose every agent turn was undelivered must not score as
    a success — that is precisely the bug that hid for a whole sprint.
    """
    channel = _channel(settings)
    transcript = TranscriptLogger("CA_silent")
    transcript.log_utterance(Speaker.CALLER, "Hello?")
    transcript.log_utterance(Speaker.AGENT, "Hi there", state="undelivered")
    transcript.log_utterance(Speaker.AGENT, "Can you hear me?", state="undelivered")

    assert channel._classify_call_failure(None, transcript, completed=True) is FailureCode.NO_AUDIO


def test_partially_delivered_call_is_not_no_audio(settings):
    """One undelivered turn in an otherwise audible call is not a silent call."""
    channel = _channel(settings)
    transcript = TranscriptLogger("CA_mixed")
    transcript.log_utterance(Speaker.AGENT, "Hi", state="greeting")
    transcript.log_utterance(Speaker.AGENT, "Lost this one", state="undelivered")
    assert channel._classify_call_failure(None, transcript, completed=True) is FailureCode.NONE


def test_call_with_no_agent_turns_is_not_no_audio(settings):
    """Nothing was said, so nothing failed to be heard."""
    channel = _channel(settings)
    transcript = TranscriptLogger("CA_empty")
    transcript.log_utterance(Speaker.CALLER, "Hello?")
    assert channel._classify_call_failure(None, transcript, completed=True) is FailureCode.NONE


# ── Cost record written at call end ──────────────────────────────────


async def test_call_end_writes_a_cost_row(settings):
    channel = _channel(settings)
    state = CallState(call_sid="CA_cost", direction=CallDirection.OUTBOUND, caller_number="+4915100000001")
    state.engine_type = "conversation_relay"
    state.language = "de"

    call_costs.begin_call("CA_cost")
    with call_costs.call_context("CA_cost"):
        call_costs.attribute_llm_cost(1000, 500, 0.02)

    await channel._record_call_telemetry(
        "CA_cost", state, FailureCode.NONE, completed=True, summary={"tts_characters": 1200}
    )

    stored = await call_costs.get_call_cost(settings, "CA_cost")
    assert stored is not None
    assert stored["llm_usd"] == pytest.approx(0.02)
    assert stored["language"] == "de"
    # ConversationRelay bundles TTS, so the characters are deliberately not billed.
    assert stored["tts_usd"] == 0.0


async def test_telemetry_failure_never_propagates(settings, monkeypatch):
    """A metrics or pricing failure must not disturb the post-call pipeline."""

    def _boom(*_a, **_kw):
        raise RuntimeError("pricing exploded")

    monkeypatch.setattr("pincer.observability.call_costs.price_call", _boom)
    channel = _channel(settings)
    state = CallState(call_sid="CA_boom", direction=CallDirection.INBOUND, caller_number="+1")

    await channel._record_call_telemetry("CA_boom", state, FailureCode.NONE, completed=True, summary=None)
    # No exception is the assertion.


# ── The full chain, through the harness ──────────────────────────────


async def _persist_scenario(settings, scenario: Scenario) -> str:
    """Run a harness scenario with a post-call processor that persists."""
    from pincer.voice.postcall import PostCallProcessor

    processor = PostCallProcessor(settings, llm=None, memory=None, db_path=settings.db_path)
    await run_scenario(scenario, post_call_processor=processor)
    return f"CA_harness_{scenario.name}"


async def _failure_code(settings, call_sid: str) -> str | None:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT failure_code FROM voice_calls WHERE call_sid = ?", (call_sid,))
        row = await cursor.fetchone()
    return str(row["failure_code"]) if row else None


async def test_successful_call_persists_failure_code_none(settings):
    sid = await _persist_scenario(settings, Scenario("cooperative", CooperativePersona, expects_task_done=True))
    assert await _failure_code(settings, sid) == FailureCode.NONE


@pytest.mark.parametrize(
    ("name", "persona"),
    [
        ("silent_timeout", SilentPersona),
        ("voicemail", VoicemailPersona),
        ("hangs_up_mid_call", HangsUpMidCallPersona),
        ("hostile", HostilePersona),
    ],
)
async def test_every_terminated_call_gets_a_code(settings, name, persona):
    """No call may reach the database with an empty failure_code — an
    unclassified call is invisible to every signal and every digest."""
    sid = await _persist_scenario(settings, Scenario(name, persona))
    code = await _failure_code(settings, sid)
    assert code, f"{name} persisted no failure_code"
    assert code in {str(c) for c in FailureCode}


async def test_golden_signals_see_harness_calls(settings):
    """The chain end to end: harness calls -> DB -> signal."""
    from pincer.observability.golden_signals import call_success_rate

    for i in range(6):
        await _persist_scenario(settings, Scenario(f"cooperative{i}", CooperativePersona, expects_task_done=True))

    signal = await call_success_rate(settings, window_hours=1)
    assert signal.sample_size == 6
    assert signal.value == 1.0
    assert signal.sufficient_data


async def test_turn_latency_metric_emission_does_not_break_a_turn(settings, monkeypatch):
    """Metric emission is wrapped, but prove it: a broken backend must not
    take a live call down."""
    monkeypatch.setattr(
        "pincer.observability.metrics.record_turn_latency",
        MagicMock(side_effect=RuntimeError("collector down")),
    )
    result = await run_scenario(Scenario("cooperative", CooperativePersona, expects_task_done=True))
    assert result.ok
