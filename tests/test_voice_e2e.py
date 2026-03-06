"""End-to-end voice call flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.base import ChannelType
from pincer.channels.phone_calls import VoiceChannel
from pincer.voice.compliance import ComplianceChecker, ConsentMode
from pincer.voice.engine import CallDirection, ConversationRelayEngine
from pincer.voice.pii_guard import mask_pii
from pincer.voice.safety_gates import (
    ConfirmationStatus,
    create_gate,
    parse_confirmation,
)
from pincer.voice.state_machine import CallPhase, CallStateMachine
from pincer.voice.transcript import Speaker, TranscriptLogger
from pincer.voice.voice_tools import filter_voice_tools, is_voice_compatible


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.twilio_account_sid = "ACtest123"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = "test_token"
    settings.twilio_phone_number = "+14155551234"
    settings.voice_engine = "conversation_relay"
    settings.voice_webhook_base_url = "https://example.com"
    settings.voice_language = "en-US"
    settings.voice_consent_mode = "one_party"
    settings.voice_recording_enabled = False
    settings.voice_max_call_duration = 600
    settings.deepgram_api_key = MagicMock()
    settings.deepgram_api_key.get_secret_value.return_value = ""
    settings.elevenlabs_api_key = MagicMock()
    settings.elevenlabs_api_key.get_secret_value.return_value = ""
    settings.elevenlabs_voice_id = ""
    return settings


class TestInboundSimpleQA:
    """Scenario 1: Call -> greet -> ask question -> get answer -> hang up."""

    async def test_inbound_qa_flow(self, mock_settings):
        engine = ConversationRelayEngine(mock_settings)
        sm = CallStateMachine("CA001")
        transcript = TranscriptLogger("CA001")

        state = await engine.on_call_start("CA001", "+15559990001", CallDirection.INBOUND)
        assert state.call_sid == "CA001"

        sm.start_call()
        assert sm.phase == CallPhase.GREETING
        transcript.log_utterance(Speaker.AGENT, "Hey! What can I help you with?")

        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")
        transcript.log_utterance(Speaker.CALLER, "What's the weather today?")

        sm.transition(CallPhase.FREEFORM, "general question")
        transcript.log_utterance(Speaker.AGENT, "It's sunny and 72 degrees in San Francisco.")

        sm.transition(CallPhase.ENDING, "caller satisfied")
        transcript.log_utterance(Speaker.AGENT, "Anything else I can help with?")
        transcript.log_utterance(Speaker.CALLER, "No, thanks!")

        sm.transition(CallPhase.COMPLETED, "call ended")
        assert sm.is_terminal

        summary = transcript.generate_summary()
        assert "CA001" in summary
        assert len(transcript.entries) == 5  # agent greet, caller Q, agent A, agent ending, caller bye


class TestInboundWithAction:
    """Scenario 3: Call -> action -> VERIFY -> confirm -> EXECUTE -> CONFIRM."""

    async def test_inbound_action_flow(self, mock_settings):
        sm = CallStateMachine("CA003")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")

        sm.set_pending_action(
            "calendar_create",
            {"title": "Team standup", "start_time": "2026-03-07T10:00:00"},
            "schedule a team standup for tomorrow at 10am",
        )
        sm.transition(CallPhase.VERIFY, "action identified")
        assert sm.phase == CallPhase.VERIFY

        gate = create_gate(
            "calendar_create",
            {"title": "Team standup"},
            "schedule a team standup for tomorrow at 10am",
        )
        confirmation = parse_confirmation("yes, go ahead")
        assert confirmation == ConfirmationStatus.CONFIRMED

        sm.confirm_action()
        sm.transition(CallPhase.EXECUTE, "user confirmed")
        assert sm.phase == CallPhase.EXECUTE

        sm.transition(CallPhase.CONFIRM, "tool completed")
        sm.transition(CallPhase.ENDING, "nothing else")
        sm.transition(CallPhase.COMPLETED, "goodbye")
        assert sm.is_terminal


class TestBargeInScenario:
    """Scenario 6: Agent speaking -> user interrupts -> agent stops."""

    async def test_barge_in(self, mock_settings):
        engine = ConversationRelayEngine(mock_settings)
        state = await engine.on_call_start("CA006", "+15559990006", CallDirection.INBOUND)

        mock_ws = AsyncMock()
        state.metadata["websocket"] = mock_ws

        await engine.send_speech("CA006", "Let me tell you about your schedule today...")
        mock_ws.send_text.assert_called()

        await engine.interrupt_speech("CA006")
        assert mock_ws.send_text.call_count >= 2


class TestErrorRecovery:
    """Scenario 7: Tool fails mid-call -> agent recovers."""

    async def test_error_recovery_flow(self):
        sm = CallStateMachine("CA007")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")
        sm.transition(CallPhase.VERIFY, "action")
        sm.transition(CallPhase.EXECUTE, "confirmed")

        sm.record_error()
        sm.transition(CallPhase.ERROR_RECOVERY, "tool failed")
        assert sm.phase == CallPhase.ERROR_RECOVERY

        sm.transition(CallPhase.INTENT_CAPTURE, "trying again")
        assert sm.phase == CallPhase.INTENT_CAPTURE


class TestCrossChannelMemory:
    """Scenario 8: Voice session shares memory context."""

    def test_voice_channel_type_in_enum(self):
        assert ChannelType.VOICE == "voice"
        assert ChannelType.VOICE in ChannelType.__members__.values()


class TestTranscriptPIIMasking:
    """Transcript with PII is masked before storage."""

    def test_pii_in_transcript_masked(self):
        transcript = TranscriptLogger("CA_PII")
        raw = "My card number is 4111 1111 1111 1111 and SSN is 123-45-6789"
        masked = mask_pii(raw)
        transcript.log_utterance(Speaker.CALLER, masked)

        entry = transcript.entries[0]
        assert "4111 **** **** 1111" in entry.text
        assert "[SSN_REDACTED]" in entry.text


class TestVoiceToolFiltering:
    def test_allowed_tools(self):
        assert is_voice_compatible("calendar_today")
        assert is_voice_compatible("web_search")
        assert is_voice_compatible("make_phone_call")

    def test_excluded_tools(self):
        assert not is_voice_compatible("shell_exec")
        assert not is_voice_compatible("python_exec")
        assert not is_voice_compatible("file_write")

    def test_skill_tools_allowed(self):
        assert is_voice_compatible("phone_contacts__search_contacts")

    def test_filter_schemas(self):
        schemas = [
            {"name": "web_search"},
            {"name": "shell_exec"},
            {"name": "calendar_today"},
            {"name": "python_exec"},
        ]
        filtered = filter_voice_tools(schemas)
        names = [s["name"] for s in filtered]
        assert "web_search" in names
        assert "calendar_today" in names
        assert "shell_exec" not in names
        assert "python_exec" not in names


class TestComplianceIntegration:
    def test_inbound_compliance_check(self, mock_settings):
        checker = ComplianceChecker(mock_settings)
        result = checker.check_inbound_call("+14155551234")
        assert result.mode == ConsentMode.TWO_PARTY
        assert result.jurisdiction == "US-two-party"

    def test_outbound_compliance_check(self, mock_settings):
        checker = ComplianceChecker(mock_settings)
        result = checker.check_outbound_call("+442012345678")
        assert result.jurisdiction == "UK"
