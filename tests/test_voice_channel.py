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
    engine.set_on_barge_in = MagicMock()
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
        mock_engine.send_speech.assert_called_once_with("CA123", "Hello!", last=True)

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
        mock_engine.send_speech.assert_called_once_with("CA456", "Found you!", last=True)

    async def test_send_no_active_call_raises(self, channel, mock_engine):
        """No active call for the user must raise, not silently no-op (issue #162)."""
        handler = AsyncMock()
        await channel.start(handler)
        with pytest.raises(RuntimeError, match="No active call"):
            await channel.send("+9999", "Hello?")
        mock_engine.send_speech.assert_not_called()

    def test_engine_callbacks_set(self, channel, mock_engine):
        mock_engine.set_on_speech.assert_called_once()
        mock_engine.set_on_call_end.assert_called_once()

    def test_state_machine_management(self, channel):
        from pincer.voice.state_machine import CallStateMachine

        sm = CallStateMachine("CA123")
        channel.set_state_machine("CA123", sm)
        assert channel.get_state_machine("CA123") is sm
        assert channel.get_state_machine("CA999") is None


async def test_a_late_utterance_after_hangup_leaves_no_speech_lock():
    """A Deepgram final / CR prompt that lands just after the call ended."""
    from voice_harness.fake_engine import FakeVoiceEngine
    from voice_harness.settings import apply_test_paths

    settings = apply_test_paths(MagicMock())
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de"
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    handler = AsyncMock(return_value="Sure.")
    await channel.start(handler)
    await engine.on_call_start("CA_late", "+4930123456", CallDirection.INBOUND, language="en")
    await channel._handle_speech("CA_late", "Hello?")
    await engine.end_call("CA_late")
    assert "CA_late" not in channel._speech_locks

    await channel._handle_speech("CA_late", "Bye!")

    assert "CA_late" not in channel._speech_locks
    await channel.stop()


SENTENCE = "The appointment is on Monday at ten."


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        (SENTENCE, "freeform"),  # the provider says all of it played
        ("The appointment is", "interrupted"),  # cut partway
    ],
)
async def test_a_sentence_cancelled_mid_send_is_marked_by_what_the_caller_heard(heard, expected):
    """ConversationRelay: the interrupt can be handled while the turn is still
    inside `send_speech` for a sentence Twilio has already played. The line is
    logged only as the cancelled send unwinds, after `mark_interrupted` ran, so
    it must be judged by `utteranceUntilInterrupt` too, not assumed cut."""
    import asyncio

    from voice_harness.fake_engine import FakeVoiceEngine
    from voice_harness.settings import apply_test_paths

    from pincer.voice.transcript import Speaker

    settings = apply_test_paths(MagicMock())
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de"
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    await channel.start(AsyncMock(return_value=""))
    await engine.on_call_start("CA_cr", "+4930123456", CallDirection.OUTBOUND, language="en")
    await channel._handle_speech("CA_cr", "When is it?")
    sm = channel.get_state_machine("CA_cr")
    assert sm is not None

    engine.speech_blocker = asyncio.Event()
    turn = asyncio.create_task(channel._say("CA_cr", SENTENCE, last=False, sm=sm))
    channel._active_turns["CA_cr"] = turn
    await asyncio.wait_for(engine.speech_waiting.wait(), timeout=2)

    await channel._on_barge_in("CA_cr", heard)
    with pytest.raises(asyncio.CancelledError):
        await turn

    lines = [e for e in channel.get_transcript("CA_cr").entries if e.speaker == Speaker.AGENT and e.text == SENTENCE]
    assert [e.state for e in lines] == [expected]
    engine.speech_blocker = None  # the shutdown goodbye must not wait on it
    await channel.stop()
