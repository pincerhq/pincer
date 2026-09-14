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
        # INTENT_CAPTURE is not a benign wrap-up phase: this caller really was
        # cut off mid-conversation, so it stays FAILED.
        assert sm.is_terminal and sm.phase == CallPhase.FAILED
        # The reason carries the interrupted phase, like `timeout_{phase}` does.
        assert sm.state.transitions[-1].reason == "shutdown_intent_capture"
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

    async def test_hung_postcall_report_does_not_block_shutdown(self, monkeypatch):
        """The post-call pipeline awaits a live LLM request; a hung provider
        must be cancelled at the drain timeout, not block shutdown forever."""
        import asyncio

        monkeypatch.setattr("pincer.channels.phone_calls.POSTCALL_DRAIN_TIMEOUT_S", 0.05)
        channel, engine = await _live_call("en")

        class _HungProcessor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                await asyncio.sleep(3600)

        channel.set_post_call_processor(_HungProcessor())
        await asyncio.wait_for(channel.stop(), timeout=5)

        assert "CA_shutdown" in engine.ended
        task = channel._postcall_tasks.get("CA_shutdown")
        assert task is not None
        await asyncio.wait([task], timeout=1)  # let the cancellation land
        assert task.cancelled()

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


class TestShutdownRespectsBenignPhases:
    """A deploy landing on a call that is already wrapping up cleanly must not
    be recorded as a failed call. The drain used to force every active call to
    FAILED regardless of phase, so every restart with live traffic put a step
    in the failure-rate dashboards and fired the alerts that watch them."""

    @staticmethod
    def _park_in(sm, phase: CallPhase) -> None:
        """Put the machine in `phase` directly.

        Not every wrap-up phase is reachable by a legal transition from where
        `_live_call` leaves the call, and the rules themselves are not what is
        under test here — the drain's terminal-phase choice is.
        """
        sm.state.phase = phase

    async def test_every_benign_phase_drains_as_completed(self):
        from pincer.channels.phone_calls import _BENIGN_TIMEOUT_PHASES

        for phase in sorted(_BENIGN_TIMEOUT_PHASES, key=lambda p: p.value):
            call_sid = f"CA_benign_{phase.value}"
            channel, _engine = await _live_call("de", call_sid=call_sid)
            sm = channel.get_state_machine(call_sid)
            self._park_in(sm, phase)

            await channel.stop()

            assert sm.phase == CallPhase.COMPLETED, f"{phase.value} heard a complete goodbye"
            assert sm.state.transitions[-1].reason == f"shutdown_{phase.value}"

    async def test_non_benign_phase_still_fails(self):
        """A call cut mid-action is a real failure and must stay visible."""
        channel, _engine = await _live_call("de", call_sid="CA_mid")
        sm = channel.get_state_machine("CA_mid")
        sm.transition(CallPhase.VERIFY, "test")
        sm.transition(CallPhase.EXECUTE, "test")

        await channel.stop()

        assert sm.phase == CallPhase.FAILED
        assert sm.state.transitions[-1].reason == "shutdown_execute"

    async def test_benign_drain_carries_no_failure_code(self):
        """The terminal phase decides `completed`, which zeroes the failure
        code — that code is what reaches the dashboards and the alerts."""
        from pincer.observability.failure_codes import FailureCode

        channel, _engine = await _live_call("de", call_sid="CA_conf")
        self._park_in(channel.get_state_machine("CA_conf"), CallPhase.CONFIRM)
        outcomes = []

        class _Processor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                outcomes.append((completed, state.metadata.get("failure_code")))

        channel.set_post_call_processor(_Processor())
        await channel.stop()
        for task in list(channel._postcall_tasks.values()):
            await task

        assert outcomes == [(True, str(FailureCode.NONE))]

    async def test_non_benign_drain_keeps_the_shutdown_code(self):
        from pincer.observability.failure_codes import FailureCode

        channel, _engine = await _live_call("de", call_sid="CA_exec")
        sm = channel.get_state_machine("CA_exec")
        sm.transition(CallPhase.VERIFY, "test")
        sm.transition(CallPhase.EXECUTE, "test")
        outcomes = []

        class _Processor:
            async def process(self, call_sid, state, transcript, completed, unverified):
                outcomes.append((completed, state.metadata.get("failure_code")))

        channel.set_post_call_processor(_Processor())
        await channel.stop()
        for task in list(channel._postcall_tasks.values()):
            await task

        assert outcomes == [(False, str(FailureCode.SHUTDOWN))]

    async def test_benign_call_still_hears_the_goodbye(self):
        """The terminal-phase decision must not change spoken behaviour."""
        channel, engine = await _live_call("de", call_sid="CA_spoken")
        self._park_in(channel.get_state_machine("CA_spoken"), CallPhase.CONFIRM)

        await channel.stop()

        assert engine.spoken["CA_spoken"][-1] == de_pack.PHASE_TIMEOUT_MESSAGES["error_recovery"]
        assert "CA_spoken" in engine.ended
