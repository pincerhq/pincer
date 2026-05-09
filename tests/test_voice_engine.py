"""Tests for the voice engine abstraction layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.voice.engine import (
    CallDirection,
    CallState,
    ConversationRelayEngine,
    MediaStreamEngine,
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
