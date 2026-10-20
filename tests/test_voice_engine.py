"""Tests for the voice engine abstraction layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.voice.engine import (
    CallDirection,
    CallState,
    ConversationRelayEngine,
    MediaStreamEngine,
    build_dial_twiml,
    get_voice_engine,
)


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
    settings.deepgram_api_key = MagicMock()
    settings.deepgram_api_key.get_secret_value.return_value = ""
    settings.elevenlabs_api_key = MagicMock()
    settings.elevenlabs_api_key.get_secret_value.return_value = ""
    settings.elevenlabs_voice_id = ""
    settings.voice_transfer_max_duration_s = 3600
    return settings


class TestConversationRelayEngine:
    @pytest.fixture
    def engine(self, mock_settings):
        return ConversationRelayEngine(mock_settings)

    async def test_engine_name(self, engine):
        assert engine.engine_name == "conversation_relay"

    async def test_call_start(self, engine):
        state = await engine.on_call_start(
            "CA123",
            "+15551234567",
            CallDirection.INBOUND,
        )
        assert state.call_sid == "CA123"
        assert state.direction == CallDirection.INBOUND
        assert state.caller_number == "+15551234567"

    async def test_get_call_state(self, engine):
        await engine.on_call_start("CA123", "+15551234567", CallDirection.INBOUND)
        state = engine.get_call_state("CA123")
        assert state is not None
        assert state.call_sid == "CA123"

    async def test_get_active_calls(self, engine):
        await engine.on_call_start("CA1", "+1111", CallDirection.INBOUND)
        await engine.on_call_start("CA2", "+2222", CallDirection.OUTBOUND)
        calls = engine.get_active_calls()
        assert len(calls) == 2

    async def test_speech_callback(self, engine):
        callback = AsyncMock()
        engine.set_on_speech(callback)
        await engine.on_call_start("CA123", "+15551234567", CallDirection.INBOUND)
        await engine.on_speech_input("CA123", "Hello there")
        callback.assert_called_once_with("CA123", "Hello there")

    async def test_send_speech_with_websocket(self, engine):
        state = await engine.on_call_start("CA123", "+1234", CallDirection.INBOUND)
        mock_ws = AsyncMock()
        state.metadata["websocket"] = mock_ws
        await engine.send_speech("CA123", "Hello!")
        mock_ws.send_text.assert_called_once()

    async def test_call_state_duration(self):
        state = CallState(
            call_sid="CA1",
            direction=CallDirection.INBOUND,
            caller_number="+1234",
        )
        assert state.duration_seconds >= 0


class _FakeTTS:
    """Minimal TTS provider double: μ-law output, records whether it ran."""

    output_format = "ulaw_8000"

    def __init__(self):
        self.synthesized = 0

    async def synthesize_stream(self, text, *, voice="", model=""):
        self.synthesized += 1
        yield b"\x00" * 160


class TestMediaStreamEngine:
    @pytest.fixture
    def engine(self, mock_settings):
        return MediaStreamEngine(mock_settings)

    async def test_engine_name(self, engine):
        assert engine.engine_name == "media_streams"

    async def test_call_start(self, engine):
        state = await engine.on_call_start(
            "CA456",
            "+15559876543",
            CallDirection.OUTBOUND,
        )
        assert state.call_sid == "CA456"
        assert state.direction == CallDirection.OUTBOUND

    async def test_send_speech_with_websocket_returns_true(self, engine):
        state = await engine.on_call_start("CA456", "+15559876543", CallDirection.OUTBOUND)
        state.metadata["websocket"] = AsyncMock()
        engine._tts_provider = _FakeTTS()
        assert await engine.send_speech("CA456", "Hello!") is True
        state.metadata["websocket"].send_text.assert_called_once()

    async def test_send_speech_without_websocket_returns_false(self, engine):
        # Honest transcripts: no socket means the caller heard nothing, so
        # this must not report delivered — and must not bill a synthesis.
        await engine.on_call_start("CA456", "+15559876543", CallDirection.OUTBOUND)
        tts = _FakeTTS()
        engine._tts_provider = tts
        assert await engine.send_speech("CA456", "Hello!") is False
        assert tts.synthesized == 0


class TestEngineFactory:
    def test_default_is_conversation_relay(self, mock_settings):
        mock_settings.voice_engine = "conversation_relay"
        engine = get_voice_engine(mock_settings)
        assert isinstance(engine, ConversationRelayEngine)

    def test_media_streams(self, mock_settings):
        mock_settings.voice_engine = "media_streams"
        engine = get_voice_engine(mock_settings)
        assert isinstance(engine, MediaStreamEngine)

    def test_unknown_defaults_to_cr(self, mock_settings):
        mock_settings.voice_engine = "unknown"
        engine = get_voice_engine(mock_settings)
        assert isinstance(engine, ConversationRelayEngine)


# ── Transfer to a human (<Dial>) ─────────────────────────────────────


def test_dial_twiml_time_limit():
    with_limit = build_dial_twiml("+4930123", timeout_s=20, time_limit_s=3600)
    assert '<Dial timeout="20" timeLimit="3600">+4930123</Dial>' in with_limit
    assert "timeLimit" not in build_dial_twiml("+4930123", timeout_s=20)


@pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
async def test_transfer_call_sets_status_callback(mock_settings, engine_cls):
    """Inbound calls get no status callback by default: without one, nothing
    tells us when the bridged caller and the human hang up."""
    engine = engine_cls(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        await engine.transfer_call("CAx", "+4930123", timeout_s=20, action_url="https://example.com/tr")
    update = client_cls.return_value.calls.return_value.update
    update.assert_called_once()
    kwargs = update.call_args.kwargs
    assert 'timeLimit="3600"' in kwargs["twiml"] and 'action="https://example.com/tr"' in kwargs["twiml"]
    assert kwargs["status_callback"] == "https://example.com/api/apps/twilio/status"
    assert kwargs["status_callback_method"] == "POST"
    assert state.metadata["transferring"] is True
    assert state.metadata["transfer"]["target"] == "+4930123"


async def test_transfer_call_without_http_base_has_no_status_callback(mock_settings):
    mock_settings.voice_webhook_base_url = ""
    engine = ConversationRelayEngine(mock_settings)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        await engine.transfer_call("CAx", "+4930123")
    kwargs = client_cls.return_value.calls.return_value.update.call_args.kwargs
    assert set(kwargs) == {"twiml"}


async def test_transfer_call_failure_leaves_call_ours(mock_settings):
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = RuntimeError("twilio down")
        with pytest.raises(RuntimeError):
            await engine.transfer_call("CAx", "+4930123")
    assert "transferring" not in state.metadata


async def test_finish_remote_ended_is_idempotent_and_never_hangs_up(mock_settings):
    engine = ConversationRelayEngine(mock_settings)
    on_end = AsyncMock()
    engine.set_on_call_end(on_end)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        assert await engine.finish_remote_ended("CAx", "transferred") is True
        assert await engine.finish_remote_ended("CAx", "provider_completed") is False
    client_cls.assert_not_called()
    on_end.assert_awaited_once()
    assert on_end.call_args.args[1].metadata["remote_end_reason"] == "transferred"
