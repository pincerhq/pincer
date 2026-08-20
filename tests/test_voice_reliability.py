"""Unit tests for Sprint 1 voice reliability hardening.

Covers: phase-timeout spoken exits + force_terminal, the VoiceChannel
watchdog / brain-error escalation / call-end guarantees, latency metrics,
live status notifications, AMD voicemail handling, and the post-call
truthfulness assertion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import MAX_CONSECUTIVE_ERRORS, VoiceChannel
from pincer.voice import status_notify
from pincer.voice.metrics import CallMetrics, VoiceMetricsRegistry
from pincer.voice.state_machine import (
    PHASE_TIMEOUT_MESSAGES,
    PHASE_TIMEOUTS,
    CallPhase,
    CallStateMachine,
)
from pincer.voice.transcript import Speaker, TranscriptLogger
from pincer.voice.twiml_server import init_voice_routes, twilio_router


@pytest.fixture(autouse=True)
def _clean_status_notify():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


# ── State machine: timeouts & forced terminal ─────────────────────────────────


class TestTimeoutMessages:
    def test_every_timed_phase_has_a_spoken_exit(self):
        for phase in PHASE_TIMEOUTS:
            if phase == CallPhase.RINGING:
                continue  # nobody is on the line yet
            sm = CallStateMachine("CA1")
            sm._state.phase = phase
            message = sm.get_timeout_message()
            assert message and message.strip(), f"{phase} timeout would be silence"
            assert "goodbye" in message.lower(), f"{phase} exit does not end the call politely"

    def test_messages_cover_all_defined_phases(self):
        for phase in PHASE_TIMEOUT_MESSAGES:
            assert PHASE_TIMEOUT_MESSAGES[phase].strip()


class TestForceTerminal:
    def test_force_terminal_from_any_phase(self):
        for phase in CallPhase:
            sm = CallStateMachine("CA1")
            sm._state.phase = phase
            sm.force_terminal(CallPhase.FAILED, reason="test")
            assert sm.is_terminal

    def test_force_terminal_records_transition(self):
        sm = CallStateMachine("CA1")
        sm.start_call()
        sm.force_terminal(CallPhase.FAILED, reason="hangup")
        assert sm.state.transitions[-1].reason == "hangup"
        assert sm.state.transitions[-1].to_phase == CallPhase.FAILED

    def test_force_terminal_rejects_non_terminal_target(self):
        sm = CallStateMachine("CA1")
        sm.force_terminal(CallPhase.EXECUTE, reason="bogus")
        assert sm.phase == CallPhase.FAILED

    def test_force_terminal_noop_when_already_terminal(self):
        sm = CallStateMachine("CA1")
        sm.force_terminal(CallPhase.COMPLETED, reason="done")
        sm.force_terminal(CallPhase.FAILED, reason="late")
        assert sm.phase == CallPhase.COMPLETED


# ── Metrics ───────────────────────────────────────────────────────────────────


class TestCallMetrics:
    def test_turn_latency_recorded(self):
        m = CallMetrics("CA1")
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        assert len(m.turn_latencies_s) == 1
        assert m.mean_turn_latency_s is not None

    def test_first_word_recorded_once(self):
        m = CallMetrics("CA1")
        m.mark_agent_speech_start()
        first = m.first_agent_word_s
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        assert m.first_agent_word_s == first

    def test_registry_summary(self):
        reg = VoiceMetricsRegistry()
        m = reg.start_call("CA1", engine="fake")
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        summary = reg.finish_call("CA1")
        assert summary is not None
        assert summary["engine"] == "fake"
        assert summary["turns"] == 1


# ── Status notifications (T1.5) ──────────────────────────────────────────────


class TestStatusNotify:
    async def test_three_stages_max_and_dedupe(self):
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA1", "user1", channel="telegram", target_number="+491234")

        assert await status_notify.notify_dialing("CA1")
        assert not await status_notify.notify_dialing("CA1")  # dedupe
        assert await status_notify.notify_connected("CA1")
        assert await status_notify.notify_ended("CA1", "completed (42s)")
        assert not await status_notify.notify_ended("CA1", "again")  # untracked after end

        assert len(sent) == 3
        assert sent[0].startswith("📞 Dialing")
        assert sent[1] == "📞 Connected"
        assert "completed (42s)" in sent[2]

    async def test_no_notifier_is_noop(self):
        status_notify.register_outbound_call("CA1", "user1")
        assert not await status_notify.notify_dialing("CA1")

    async def test_unknown_call_is_noop(self):
        async def notifier(user_id, channel, text):
            return True

        status_notify.set_status_notifier(notifier)
        assert not await status_notify.notify_connected("CA_unknown")


# ── Truthfulness assertion (T1.4) ────────────────────────────────────────────


class TestCompletionClaims:
    def test_claim_without_action_is_flagged(self, caplog):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "Great, your appointment is booked for Tuesday.")
        with caplog.at_level("WARNING"):
            claims = t.verify_completion_claims()
        assert len(claims) == 1
        assert "Unverified completion claim" in caplog.text

    def test_claim_with_successful_action_passes(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "All set — I've booked it for Tuesday at three.")
        t.log_action("tool_call", "calendar_create", output_summary="Event created: link")
        assert t.verify_completion_claims() == []

    def test_claim_with_error_action_is_flagged(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "It's done, I've sent the email.")
        t.log_action("tool_call", "email_send", output_summary="Error: SMTP timeout")
        assert len(t.verify_completion_claims()) == 1

    def test_caller_claims_are_ignored(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.CALLER, "I've booked it myself already.")
        assert t.verify_completion_claims() == []

    def test_no_claim_no_flag(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "I'm calling about the appointment. Is Tuesday okay?")
        assert t.verify_completion_claims() == []


# ── VoiceChannel hardening ────────────────────────────────────────────────────


def _make_channel():
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = apply_test_paths(MagicMock())
    settings.voice_language = "en-US"
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    return channel, engine


class TestVoiceChannelHardening:
    async def test_phase_timeout_speaks_and_ends(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Hello! How can I help?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)
        sm = channel._ensure_call_tracking("CA1", state)

        sm._state.phase_entered_at -= PHASE_TIMEOUTS[sm.phase] + 1
        assert sm.check_timeout()
        await channel._handle_phase_timeout("CA1", sm)

        assert sm.is_terminal
        assert "CA1" in engine.ended
        assert any("goodbye" in s.lower() for s in engine.spoken["CA1"])
        await channel.stop()

    async def test_brain_errors_escalate_to_graceful_end(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        failing_handler = AsyncMock(side_effect=RuntimeError("LLM down"))
        await channel.start(failing_handler)
        await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)

        for _ in range(MAX_CONSECUTIVE_ERRORS):
            await channel._handle_speech("CA1", "Hello?")

        sm_phase_seen = False
        for spoken in engine.spoken["CA1"]:
            if "goodbye" in spoken.lower():
                sm_phase_seen = True
        assert sm_phase_seen, "no spoken apology/goodbye on repeated errors"
        assert "CA1" in engine.ended
        await channel.stop()

    async def test_call_end_forces_terminal_and_notifies(self):
        from pincer.voice.engine import CallDirection

        sent: list[str] = []

        async def notifier(user_id, channel_name, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA1", "user1", channel="telegram")

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Okay. Anything else?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.OUTBOUND)
        sm = channel._ensure_call_tracking("CA1", state)
        assert not sm.is_terminal

        # Callee hangs up mid-call
        await engine.end_call("CA1")

        assert sm.is_terminal
        assert channel.get_state_machine("CA1") is None
        assert any(msg.startswith("📞 Call ended") for msg in sent)
        await channel.stop()

    async def test_greeting_advances_on_speech(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Sure. What time works?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)
        sm = channel._ensure_call_tracking("CA1", state)
        assert sm.phase == CallPhase.GREETING

        await channel._handle_speech("CA1", "Hi, I need to book something.")
        assert sm.phase == CallPhase.INTENT_CAPTURE
        await channel.stop()


# ── AMD / status webhook (T1.3) ──────────────────────────────────────────────


@pytest.fixture
def webhook_client():
    from fastapi import FastAPI

    engine = AsyncMock()
    engine.get_active_calls = MagicMock(return_value={})
    engine.get_call_state = MagicMock(return_value=None)
    settings = MagicMock()
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token.get_secret_value.return_value = ""
    init_voice_routes(engine, settings)
    app = FastAPI()
    app.include_router(twilio_router)
    return TestClient(app), engine


class TestAnsweringMachineDetection:
    def _setup_notify(self) -> list[str]:
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA_amd", "user1", channel="telegram")
        return sent

    def test_machine_detected_hangs_up_and_reports(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_called_once_with("CA_amd")
        assert any("voicemail" in msg for msg in sent)

    def test_machine_verdict_ignored_when_caller_already_spoke(self, webhook_client):
        """AMD false positive: a late 'machine_start' must not kill a live
        conversation (hotfix — a real call died 20s in exactly this way)."""
        from pincer.voice.engine import CallDirection, CallState

        client, engine = webhook_client
        sent = self._setup_notify()

        state = CallState(call_sid="CA_amd", direction=CallDirection.OUTBOUND, caller_number="+1")
        state.metadata["caller_spoke"] = True
        engine.get_call_state = MagicMock(return_value=state)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_not_called()
        assert not any("voicemail" in msg for msg in sent)
        assert "📞 Connected" in sent  # falls through to normal in-progress handling

    def test_human_answer_notifies_connected(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "human"},
        )
        assert response.status_code == 200
        engine.end_call.assert_not_called()
        assert "📞 Connected" in sent

    def test_no_answer_reports_final_status(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "no-answer"},
        )
        assert response.status_code == 200
        assert any("no answer" in msg for msg in sent)


class TestConversationRelayProtocol:
    """CR protocol hygiene (hotfix): no Media-Streams messages on the CR socket."""

    async def test_interrupt_sends_nothing_over_websocket(self):
        # {"type": "clear"} is invalid on ConversationRelay (Twilio 64107);
        # Twilio handles barge-in itself, so interrupt must be a local no-op.
        from pincer.voice.engine import CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr", "+1", CallDirection.OUTBOUND)
        ws = AsyncMock()
        state.metadata["websocket"] = ws

        await engine.interrupt_speech("CA_cr")
        ws.send_text.assert_not_called()

    async def test_speech_input_marks_caller_spoke(self):
        from pincer.voice.engine import CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr2", "+1", CallDirection.OUTBOUND)
        assert "caller_spoke" not in state.metadata
        await engine.on_speech_input("CA_cr2", "Hello?")
        assert state.metadata["caller_spoke"] is True

    async def test_send_speech_reports_delivery(self):
        """F4: True only when the token reached the socket — silence is never
        reported as success."""
        from pincer.voice.engine import CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr3", "+1", CallDirection.OUTBOUND)

        # No websocket attached yet → dropped
        assert await engine.send_speech("CA_cr3", "hello") is False

        ws = AsyncMock()
        state.metadata["websocket"] = ws
        assert await engine.send_speech("CA_cr3", "hello") is True
        ws.send_text.assert_called_once()

        ws.send_text.side_effect = RuntimeError("socket closed")
        assert await engine.send_speech("CA_cr3", "hello") is False

        # Unknown call
        assert await engine.send_speech("CA_nope", "hello") is False


class TestUndeliveredTranscript:
    """F4: the transcript distinguishes 'text generated' from 'audio heard'."""

    async def test_undelivered_agent_turn_marked(self):
        from pincer.voice.engine import CallDirection, VoiceEngine

        class SilentEngine(VoiceEngine):
            """send_speech always fails to deliver (no socket)."""

            @property
            def engine_name(self):
                return "silent"

            async def on_call_start(self, call_sid, caller, direction, **kwargs):
                return await self._register_call(call_sid, caller, direction)

            async def on_speech_input(self, call_sid, text_or_audio):
                pass

            async def send_speech(self, call_sid, text_or_audio):
                return False

            async def interrupt_speech(self, call_sid):
                pass

            async def transfer_call(self, call_sid, target_number):
                pass

            async def end_call(self, call_sid):
                await self._unregister_call(call_sid)

            async def send_dtmf(self, call_sid, digits):
                pass

            async def close_media_stream(self, call_sid):
                pass

        settings = apply_test_paths(MagicMock())
        settings.voice_default_language = "en"
        settings.voice_supported_languages = "en,de,uk"
        settings.voice_language = "en-US"
        channel = VoiceChannel(settings)
        engine = SilentEngine(settings)
        channel.set_engine(engine)

        async def handler(incoming):
            return "I have booked your appointment."

        await channel.start(handler)
        await engine.on_call_start("CA_silent", "+1", CallDirection.OUTBOUND)
        await channel._handle_speech("CA_silent", "Please book it.")

        transcript = channel.get_transcript("CA_silent")
        agent_entries = [e for e in transcript.entries if e.speaker == Speaker.AGENT]
        assert agent_entries and agent_entries[-1].state == "undelivered"
        await channel.stop()


# ── Outbound AMD parameter ────────────────────────────────────────────────────


class TestOutboundMachineDetection:
    async def test_machine_detection_param_sent(self, monkeypatch, tmp_path):
        from pincer.voice import outbound

        settings = MagicMock()
        settings.voice_enabled = True
        settings.voice_outbound_enabled = True
        settings.voice_webhook_base_url = "https://example.com"
        settings.voice_outbound_max_daily = 10
        settings.voice_max_call_duration = 600
        settings.voice_engine = "conversation_relay"
        settings.voice_language = "en-US"
        settings.voice_machine_detection = True
        settings.voice_consent_mode = "none"
        settings.voice_recording_enabled = False
        settings.voice_consent_language = ""
        settings.voice_assistant_name = ""
        settings.voice_intro_text = ""
        # Sprint 8: the outbound gate persists its call log here; without a real
        # path the MagicMock stringifies into a junk file in the repo root.
        settings.db_path = str(tmp_path / "pincer.db")
        settings.twilio_account_sid = "AC123"
        settings.twilio_auth_token.get_secret_value.return_value = "token"
        settings.twilio_phone_number = "+15550001111"
        monkeypatch.setattr("pincer.config.get_settings", lambda: settings)

        captured: dict = {}

        class FakeCalls:
            def create(self, **kwargs):
                captured.update(kwargs)
                call = MagicMock()
                call.sid = "CA_out"
                return call

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.calls = FakeCalls()

        import sys
        import types

        twilio_rest = types.ModuleType("twilio.rest")
        twilio_rest.Client = FakeClient
        twilio_mod = types.ModuleType("twilio")
        twilio_mod.rest = twilio_rest
        monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
        monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)

        result = await outbound.make_phone_call("+14155551234", "test purpose", context={"user_id": "u1"})
        assert "Call initiated" in result
        assert captured.get("machine_detection") == "Enable"
