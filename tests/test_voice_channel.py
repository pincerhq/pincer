"""Tests for the Voice channel integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.base import ChannelType
from pincer.channels.phone_calls import VoiceChannel
from pincer.voice.engine import CallDirection, CallState


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.voice_enabled = True
    settings.twilio_account_sid = "ACtest123"
    return settings


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.get_active_calls = MagicMock(return_value={})
    engine.get_call_state = MagicMock(return_value=None)
    engine.set_on_speech = MagicMock()
    engine.set_on_call_end = MagicMock()
    return engine


@pytest.fixture
def channel(mock_settings, mock_engine):
    ch = VoiceChannel(mock_settings)
    ch.set_engine(mock_engine)
    return ch


class TestVoiceChannel:
    def test_channel_type(self, channel):
        assert channel.channel_type == ChannelType.VOICE

    def test_name(self, channel):
        assert channel.name == "voice"

    async def test_start(self, channel):
        handler = AsyncMock()
        await channel.start(handler)
        assert channel._handler is handler

    async def test_stop(self, channel, mock_engine):
        handler = AsyncMock()
        await channel.start(handler)
        await channel.stop()

    async def test_send_with_call_sid(self, channel, mock_engine):
        handler = AsyncMock()
        await channel.start(handler)
        await channel.send("+1234", "Hello!", call_sid="CA123")
        mock_engine.send_speech.assert_called_once_with("CA123", "Hello!")

    async def test_send_finds_active_call(self, channel, mock_engine):
        state = CallState(
            call_sid="CA456",
            direction=CallDirection.INBOUND,
            caller_number="+1234",
        )
        mock_engine.get_active_calls.return_value = {"CA456": state}

        handler = AsyncMock()
        await channel.start(handler)
        await channel.send("+1234", "Found you!")
        mock_engine.send_speech.assert_called_once_with("CA456", "Found you!")

    def test_engine_callbacks_set(self, channel, mock_engine):
        mock_engine.set_on_speech.assert_called_once()
        mock_engine.set_on_call_end.assert_called_once()

    def test_state_machine_management(self, channel):
        from pincer.voice.state_machine import CallStateMachine

        sm = CallStateMachine("CA123")
        channel.set_state_machine("CA123", sm)
        assert channel.get_state_machine("CA123") is sm
        assert channel.get_state_machine("CA999") is None
