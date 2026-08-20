"""Graceful shutdown drain (Sprint 7, T7.2) — a deploy/SIGTERM during an
active call must produce a spoken, localized ending and a post-call report,
never dead air."""

from __future__ import annotations

from unittest.mock import MagicMock

from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice.engine import CallDirection
from pincer.voice.prompts import de as de_pack
from pincer.voice.prompts import en as en_pack
from pincer.voice.state_machine import CallPhase
from pincer.voice.transcript import Speaker


def _settings():
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    return settings


async def _handler(_incoming):
    return "Gerne, einen Moment."


async def _live_call(language: str, call_sid: str = "CA_shutdown"):
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    await channel.start(_handler)
    await engine.on_call_start(call_sid, "+4930123456", CallDirection.OUTBOUND, language=language)
    await engine.on_speech_input(call_sid, "Guten Tag?")  # creates sm + transcript
    return channel, engine


class TestGracefulShutdown:
    async def test_active_call_hears_localized_ending_before_hangup(self):
        channel, engine = await _live_call("de")
        transcript = channel.get_transcript("CA_shutdown")
        sm = channel.get_state_machine("CA_shutdown")

        await channel.stop()

        goodbye = de_pack.PHASE_TIMEOUT_MESSAGES["error_recovery"]
        assert engine.spoken["CA_shutdown"][-1] == goodbye  # spoken, in German
        assert "CA_shutdown" in engine.ended  # then hung up
        assert sm.is_terminal and sm.phase == CallPhase.FAILED
        assert sm.state.transitions[-1].reason == "shutdown"
        agent_lines = [e for e in transcript.entries if e.speaker == Speaker.AGENT and e.state == "shutdown"]
        assert len(agent_lines) == 1 and agent_lines[0].text == goodbye

    async def test_english_call_gets_english_ending(self):
        channel, engine = await _live_call("en")
        await channel.stop()
        assert engine.spoken["CA_shutdown"][-1] == en_pack.PHASE_TIMEOUT_MESSAGES["error_recovery"]

    async def test_initiator_receives_failure_report(self):
        """The post-call pipeline runs (and is awaited) for calls cut by the
        shutdown — the initiating user must learn the call did not finish."""
        channel, engine = await _live_call("de")
        processed = []

        class _Processor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                processed.append((call_sid, completed))

        channel.set_post_call_processor(_Processor())
        await channel.stop()

        assert processed == [("CA_shutdown", False)]  # shutdown => not completed

    async def test_stop_without_active_calls_is_clean(self):
        from voice_harness.fake_engine import FakeVoiceEngine

        settings = _settings()
        engine = FakeVoiceEngine(settings)
        channel = VoiceChannel(settings)
        channel.set_engine(engine)
        await channel.start(_handler)
        await channel.stop()
        assert engine.ended == []


class TestPhaseInactivityClock:
    async def test_caller_activity_prevents_two_minute_cutoff(self, monkeypatch):
        """Regression: an ACTIVE conversation in INTENT_CAPTURE must never be
        cut by the 120s phase timeout — caller speech resets the clock."""
        import time as time_mod

        channel, engine = await _live_call("en", call_sid="CA_active")
        sm = channel.get_state_machine("CA_active")

        # Simulate being deep into the phase, past the 120s timeout
        sm.state.phase_entered_at = time_mod.monotonic() - 300
        assert sm.check_timeout() is True

        # Caller speaks -> clock resets -> watchdog leaves the call alone
        await engine.on_speech_input("CA_active", "One more thing, please.")
        assert sm.check_timeout() is False
        await channel.stop()


class TestHangupSemantics:
    async def test_hangup_mid_conversation_is_a_normal_ending(self):
        """'Thanks, bye' + click in INTENT_CAPTURE must count as COMPLETED —
        not failed (the 83s-conversation-marked-failed bug)."""
        channel, engine = await _live_call("en", call_sid="CA_bye")
        outcomes = []

        class _Processor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                outcomes.append(completed)

        channel.set_post_call_processor(_Processor())
        await engine.end_call("CA_bye")  # caller hung up mid intent_capture
        for task in list(channel._postcall_tasks.values()):
            await task
        assert outcomes == [True]

    async def test_hangup_during_execute_still_fails(self):
        from pincer.voice.state_machine import CallPhase

        channel, engine = await _live_call("en", call_sid="CA_cut")
        sm = channel.get_state_machine("CA_cut")
        sm.transition(CallPhase.VERIFY, "test")
        sm.transition(CallPhase.EXECUTE, "test")
        outcomes = []

        class _Processor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                outcomes.append(completed)

        channel.set_post_call_processor(_Processor())
        await engine.end_call("CA_cut")  # line dropped mid-action
        for task in list(channel._postcall_tasks.values()):
            await task
        assert outcomes == [False]
