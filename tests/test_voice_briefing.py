"""Call briefing — validation, the setup race, and the binding prompt block.

The product promise of an outbound call is "the agent does what you told it".
These tests hold the three places that promise used to break: a task that was
never validated, a state that was registered too late, and a prompt block that
read as background instead of as an instruction.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from voice_harness.fake_engine import FakeVoiceEngine
from voice_harness.settings import apply_in_call_tool_defaults, apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import briefing as bf
from pincer.voice import outbound, status_notify
from pincer.voice.briefing import (
    MAX_TASK_CHARS,
    MIN_TASK_CHARS,
    TASK_TOO_LONG,
    TASK_TOO_SHORT,
    BriefingError,
    CallBriefing,
    validate_task,
)
from pincer.voice.engine import CallDirection
from pincer.voice.transcript import Speaker

TARGET = "+4930111222"
TASK = "Ask what time they close today and whether they take walk-ins."


# ── §3.1 validation matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("task", "ok"),
    [
        ("y" * (MIN_TASK_CHARS - 1), False),
        ("y" * MIN_TASK_CHARS, True),
        ("y" * MAX_TASK_CHARS, True),
        ("y" * (MAX_TASK_CHARS + 1), False),
        ("", False),
        ("   \t\n  ", False),
        ("  " + "y" * MIN_TASK_CHARS + "  ", True),  # length is measured after stripping
        (None, False),
    ],
)
def test_task_validation_matrix(task, ok):
    if ok:
        assert validate_task(task) == str(task).strip()
        return
    with pytest.raises(BriefingError) as exc:
        validate_task(task)
    assert str(exc.value) in (TASK_TOO_SHORT, TASK_TOO_LONG)


def test_validation_message_is_actionable():
    """The message is shown to the user verbatim, so it must say what to do."""
    assert "concretely what to do" in TASK_TOO_SHORT
    assert str(MAX_TASK_CHARS) in TASK_TOO_LONG


def test_briefing_create_normalises_and_caps():
    briefing = CallBriefing.create(
        "  " + TASK + "  ", target_name="Dr. Müller", language="de", source="chat", instructions="i" * 9000
    )
    assert briefing.task == TASK  # verbatim apart from the surrounding whitespace
    assert briefing.target_name == "Dr. Müller"
    assert len(briefing.instructions) == bf.MAX_INSTRUCTIONS_CHARS


def test_briefing_round_trips_through_json():
    briefing = CallBriefing.create(TASK, target_name="Dr. Müller", source="dashboard", instructions="Be brief")
    restored = CallBriefing.from_json(briefing.to_json())
    assert restored is not None
    assert restored.task == TASK
    assert restored.source == "dashboard"
    assert restored.instructions == "Be brief"

    assert CallBriefing.from_json("") is None
    assert CallBriefing.from_json("{not json") is None
    assert CallBriefing.from_json(json.dumps({"task": "   "})) is None


def test_briefing_from_state_falls_back_to_the_call_state():
    """A call registered by an older path still reports its briefing."""
    state = MagicMock()
    state.metadata = {}
    state.purpose = TASK
    state.target_name = "Dr. Müller"
    state.language = "de"
    state.instructions = ""
    recovered = bf.briefing_from_state(state)
    assert recovered is not None and recovered.task == TASK

    state.purpose = ""
    assert bf.briefing_from_state(state) is None


# ── §2 the setup race ────────────────────────────────────────────────


def _settings(tmp_path) -> MagicMock:
    cfg = apply_test_paths(MagicMock(), tmp_path)
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = True
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.voice_outbound_max_daily = 100
    cfg.voice_machine_detection = False
    cfg.voice_engine = "conversation_relay"
    cfg.voice_default_language = "en"
    cfg.voice_supported_languages = "en,de,uk"
    cfg.voice_language = "en-US"
    cfg.voice_de_formality = "sie"
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.voice_consent_mode = "one_party"
    cfg.voice_consent_language = ""
    cfg.voice_recording_enabled = False
    cfg.voice_assistant_name = "Pincer"
    cfg.voice_assistant_org = "3days.ai"
    cfg.voice_assistant_owner = "Jane Doe"
    cfg.voice_intro_text = ""
    cfg.voice_ws_auth_required = False
    cfg.twilio_account_sid = "AC123"
    cfg.twilio_auth_token.get_secret_value.return_value = "token"
    cfg.twilio_phone_number = "+4915212345678"
    cfg.voice_daily_call_limit = 20
    cfg.voice_target_cooldown_min = 0
    cfg.voice_quiet_hours = ""
    cfg.voice_quiet_hours_override_users = ""
    cfg.voice_stt_min_confidence = 0.55
    cfg.voice_filler_phrases = ""
    cfg.receptionist_enabled = False
    cfg.thread_match_window_days = 7
    cfg.thread_inbound_context = "off"
    cfg.thread_autoclose_days = 30
    cfg.dashboard_url = ""
    cfg.default_user_id = ""
    return apply_in_call_tool_defaults(cfg, default_user_id="")


@pytest.fixture
def voice(tmp_path, monkeypatch):
    """A live engine + channel, with `make_phone_call` on a fake Twilio."""
    from pincer.voice import twiml_server

    settings = _settings(tmp_path)
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)

    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr(twiml_server, "_engine", engine)
    monkeypatch.setattr(twiml_server, "_settings", settings)
    monkeypatch.setattr(outbound, "_daily_outbound_counts", {})

    placed: list[dict[str, Any]] = []
    hold = asyncio.Event()
    hold.set()

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

    status_notify._reset_for_tests()
    yield types.SimpleNamespace(
        settings=settings, engine=engine, channel=channel, placed=placed, hold=hold, server=twiml_server
    )
    status_notify._reset_for_tests()


async def test_state_is_registered_before_the_dial(voice):
    """The briefing must exist before Twilio can connect anything to it."""
    seen: list[str] = []

    original = voice.engine.register_pending_outbound

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("registered")
        return await original(*args, **kwargs)

    voice.engine.register_pending_outbound = _spy  # type: ignore[method-assign]

    class OrderedCalls:
        def create(self, **kwargs: Any) -> Any:
            seen.append("dialled")
            call = MagicMock()
            call.sid = "CA_1"
            return call

    voice.server._engine = voice.engine
    import twilio.rest as twilio_rest  # noqa: PLC0415 — the fake installed by the fixture

    twilio_rest.Client = lambda *a, **k: types.SimpleNamespace(calls=OrderedCalls())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "Call SID: CA_1" in result
    assert seen == ["registered", "dialled"]

    state = voice.engine.get_call_state("CA_1")
    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK


async def test_setup_before_registration_outbound_waits_then_binds(voice):
    """setup fires first, registration lands later → the briefing still binds.

    This is the race that produced the bug: the relay socket connected before
    `calls.create()` had returned, the handler found no state, invented an
    INBOUND one, and the agent ran a generic persona on a briefed call.
    """
    call_sid = "CA_race"

    async def _register_late() -> None:
        await asyncio.sleep(0.5)
        briefing = CallBriefing.create(TASK, target_name="Dr. Müller", source="chat")
        pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
        await voice.engine.promote_pending(pre, call_sid)

    later = asyncio.create_task(_register_late())
    state = await voice.server._resolve_setup_state(call_sid, TARGET, "outbound-api")
    await later

    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK

    sm = voice.channel._ensure_call_tracking(call_sid, state)
    assert TASK in voice.channel._build_voice_system(state, sm)


async def test_setup_orphan_outbound_terminates(voice):
    """No briefing anywhere → the call is refused, never improvised."""
    notified: list[str] = []

    async def notifier(user_id, channel, text):
        notified.append(text)
        return True

    status_notify.set_status_notifier(notifier)
    status_notify.register_outbound_call("CA_orphan", user_id="u1", channel="telegram", target_number=TARGET)

    state = await voice.server._resolve_setup_state("CA_orphan", TARGET, "outbound-api")

    assert state is None, "an outbound call with no briefing must not get a state"
    assert voice.engine.get_call_state("CA_orphan") is None
    assert notified and "could not be recovered" in notified[0]

    async with aiosqlite.connect(str(voice.settings.db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT direction, failure_code FROM voice_calls WHERE call_sid = 'CA_orphan'")
        row = await cursor.fetchone()
    assert row is not None
    assert row["direction"] == "outbound"
    assert row["failure_code"] == "briefing_lost"


async def test_setup_recovers_the_briefing_from_the_status_record(voice):
    """The pre-existing recovery path still works and still carries the task."""
    status_notify.register_outbound_call(
        "CA_recover",
        user_id="u1",
        channel="telegram",
        purpose=TASK,
        target_number=TARGET,
        language="en",
        target_name="Dr. Müller",
    )
    state = await voice.server._resolve_setup_state("CA_recover", TARGET, "outbound-api")
    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK


async def test_inbound_setup_is_untouched_by_the_termination_rule(voice):
    """A genuine inbound call has no briefing and must still be answered."""
    state = await voice.server._resolve_setup_state("CA_in", "+4917612345", "")
    assert state is not None
    assert state.direction == CallDirection.INBOUND


async def test_failed_dial_discards_the_pre_registration(voice, monkeypatch):
    """Twilio raised → no orphaned pending state left behind."""

    class ExplodingCalls:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("twilio down")

    import twilio.rest as twilio_rest  # noqa: PLC0415

    twilio_rest.Client = lambda *a, **k: types.SimpleNamespace(calls=ExplodingCalls())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error")
    assert voice.engine._pending_outbound == {}


async def test_promote_pending_is_idempotent(voice):
    briefing = CallBriefing.create(TASK)
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    first = await voice.engine.promote_pending(pre, "CA_p")
    second = await voice.engine.promote_pending(pre, "CA_p")
    assert first is second
    assert voice.engine._pending_outbound == {}


async def test_await_call_state_gives_up(voice):
    assert await voice.engine.await_call_state("CA_never", timeout=0.2, interval=0.05) is None


# ── §3.1 no unbriefed call can be placed ─────────────────────────────


@pytest.mark.parametrize("purpose", ["", "  ", "call mum", "x"])
async def test_make_phone_call_refuses_an_unusable_task(voice, purpose):
    result = await outbound.make_phone_call(TARGET, purpose, context={"user_id": "u1"})
    assert result.startswith("Error")
    assert "Purpose too short" in result
    assert voice.placed == [], "no dial may happen for a call with no task"


async def test_make_phone_call_binds_and_logs_the_briefing(voice, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="pincer.voice.briefing"):
        result = await outbound.make_phone_call(
            TARGET, TASK, target_name="Dr. Müller", context={"user_id": "u1"}, source="dashboard"
        )
    assert "Call SID: CA_1" in result
    assert any("BRIEFING_BOUND" in r.message and "source=dashboard" in r.message for r in caplog.records)


# ── §3.1 transcript audit line ───────────────────────────────────────


async def test_briefing_lands_in_the_transcript(voice):
    state = await voice.engine.on_call_start(
        "CA_t", TARGET, CallDirection.OUTBOUND, target_number=TARGET, purpose=TASK, language="en"
    )
    await voice.channel._handle_call_start("CA_t", state)

    transcript = voice.channel.get_transcript("CA_t")
    assert transcript is not None
    lines = [e.text for e in transcript.entries if e.speaker == Speaker.SYSTEM]
    assert lines and lines[0].startswith("[BRIEFING] ")
    assert TASK[:50] in lines[0]


async def test_inbound_call_gets_no_briefing_line(voice):
    state = await voice.engine.on_call_start("CA_ti", "+4917612345", CallDirection.INBOUND, language="en")
    await voice.channel._handle_call_start("CA_ti", state)
    transcript = voice.channel.get_transcript("CA_ti")
    assert transcript is not None
    assert not [e for e in transcript.entries if e.speaker == Speaker.SYSTEM]


# ── §4.2 adherence smoke detector ────────────────────────────────────


class _FakeTranscript:
    def __init__(self, agent_lines: list[str]) -> None:
        self.entries = [types.SimpleNamespace(speaker=Speaker.AGENT, text=t, is_final=True) for t in agent_lines]


def test_adherence_detects_a_capability_monologue():
    monologue = [
        "Hello, I am a digital assistant.",
        "I can help you with appointments, emails, reminders and much more.",
        "What would you like me to do for you today?",
    ]
    adhered, overlap = bf.check_adherence(TASK, _FakeTranscript(monologue))
    assert not adhered
    assert overlap < bf.ADHERENCE_THRESHOLD


def test_adherence_accepts_an_agent_that_stated_the_task():
    on_task = ["Hello, I am calling to ask what time you close today, and whether you take walk-ins."]
    adhered, _ = bf.check_adherence(TASK, _FakeTranscript(on_task))
    assert adhered


def test_adherence_defers_to_a_real_task_result():
    """A call that demonstrably did the job is adherent whatever it said."""
    adhered, _ = bf.check_adherence(
        TASK, _FakeTranscript(["Something else entirely."]), task_result="They close at 18:00."
    )
    assert adhered


def test_adherence_is_silent_when_there_is_nothing_to_judge():
    assert bf.check_adherence(TASK, None)[0] is True
    assert bf.check_adherence(TASK, _FakeTranscript([]))[0] is True
    assert bf.check_adherence("", _FakeTranscript(["anything"]))[0] is True


def test_report_adherence_warns_but_never_raises(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="pincer.voice.briefing"):
        assert bf.report_adherence("CA_x", TASK, _FakeTranscript(["I can help with many things."])) is False
    assert any("briefing_adherence_low" in r.message for r in caplog.records)


# ── Regression: registering a call is not answering it ───────────────


async def test_pre_registration_does_not_start_the_conversation_clock(voice):
    """The dial returns while the phone is still RINGING.

    Firing per-call tracking at promotion started the state machine's
    `outbound_greeting` clock during the ring; thirty seconds later the
    watchdog spoke its timeout goodbye and hung up on a call the callee had
    not answered yet. From the callee's side: they pick up and hear silence.
    """
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_ring")

    state = voice.engine.get_call_state("CA_ring")
    assert state is not None, "the briefed state must exist before the callee answers"
    assert state.metadata.get("answered_at") is None
    assert voice.channel.get_state_machine("CA_ring") is None, "no phase clock may run while the call is still ringing"
    assert voice.channel.get_transcript("CA_ring") is None


async def test_setup_marks_the_call_answered_and_starts_tracking(voice):
    """Twilio's `setup` is the answer signal — that is when clocks start."""
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_answered")

    state = await voice.engine.mark_call_answered("CA_answered")

    assert state is not None and state.metadata.get("answered_at") is not None
    sm = voice.channel.get_state_machine("CA_answered")
    assert sm is not None, "tracking starts on answer"
    assert not sm.is_terminal
    # And the briefing audit line was written at answer time.
    transcript = voice.channel.get_transcript("CA_answered")
    assert transcript is not None
    assert any(e.text.startswith("[BRIEFING]") for e in transcript.entries)


async def test_mark_call_answered_is_idempotent(voice):
    """A second `setup` (returning from a <Dial> transfer) must not restart
    the clocks — that would hand the call a fresh 30 s greeting timeout."""
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_twice")

    await voice.engine.mark_call_answered("CA_twice")
    sm_first = voice.channel.get_state_machine("CA_twice")
    first_answered = voice.engine.get_call_state("CA_twice").metadata["answered_at"]

    await voice.engine.mark_call_answered("CA_twice")

    assert voice.channel.get_state_machine("CA_twice") is sm_first
    assert voice.engine.get_call_state("CA_twice").metadata["answered_at"] == first_answered


async def test_talk_time_excludes_the_ringing(voice):
    """Ringing is not silence: the accumulator is re-anchored on answer."""
    import asyncio

    from pincer.voice.analytics import get_accumulator

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_ring2")

    await asyncio.sleep(0.05)  # stand-in for the ring
    await voice.engine.mark_call_answered("CA_ring2")

    accumulator = get_accumulator(voice.engine.get_call_state("CA_ring2"))
    assert accumulator is not None
    assert accumulator._now_ms() < 20, "the talk-time clock starts at answer, not at dial"


async def test_inbound_calls_are_answered_on_registration(voice):
    """An inbound call is already live when we first see it, so nothing defers."""
    state = await voice.engine.on_call_start("CA_in2", "+4917612345", CallDirection.INBOUND, language="en")
    assert state.metadata.get("answered_at") is not None
    assert voice.channel.get_state_machine("CA_in2") is not None
