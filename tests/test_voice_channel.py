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


async def _channel_with_fake_engine(tmp_path):
    from voice_harness.fake_engine import FakeVoiceEngine
    from voice_harness.settings import apply_test_paths

    settings = apply_test_paths(MagicMock(), tmp_path)
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de"
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    await channel.start(AsyncMock(return_value=""))
    return settings, engine, channel


async def _settle_rows(channel):
    import asyncio

    while channel._call_row_tasks:
        await asyncio.wait(set(channel._call_row_tasks.values()))


async def test_call_row_is_written_at_start_and_closed_at_teardown(tmp_path):
    """Without a post-call processor nothing else writes the history row: the
    call must still be in it, open while live and with `ended_at` once over."""
    from pathlib import Path

    from pincer.services.voice.calls import CallsService

    settings, engine, channel = await _channel_with_fake_engine(tmp_path)
    await engine.on_call_start("CA_row", "+4930123456", CallDirection.INBOUND, language="en")
    await _settle_rows(channel)
    calls = await CallsService.for_path(Path(settings.db_path))
    row = await calls.get("CA_row")
    assert row is not None
    assert row["direction"] == "inbound" and row["from_number"] == "+4930123456"
    assert row["engine"] == "fake" and row["started_at"]
    assert row["ended_at"] is None

    state = engine.get_call_state("CA_row")
    await engine.end_call("CA_row")
    await _settle_rows(channel)
    row = await calls.get("CA_row")
    assert row is not None and row["ended_at"] is not None
    assert row["failure_code"] == state.metadata["failure_code"]  # the classified code, not ""
    await channel.stop()


async def test_a_re_registered_call_keeps_its_row_start(tmp_path):
    """Lost-state recovery registers the call again with a fresh CallState:
    the row keeps its real start and numbers."""
    from pathlib import Path

    from pincer.services.voice.calls import CallsService

    settings, engine, channel = await _channel_with_fake_engine(tmp_path)
    await engine.on_call_start("CA_re", "+4930123456", CallDirection.INBOUND, language="en")
    await _settle_rows(channel)
    calls = await CallsService.for_path(Path(settings.db_path))
    first = await calls.get("CA_re")

    engine._active_calls.pop("CA_re")
    await engine.on_call_start("CA_re", "+4930999999", CallDirection.INBOUND, language="en")
    await _settle_rows(channel)
    again = await calls.get("CA_re")
    assert again["started_at"] == first["started_at"] and again["from_number"] == "+4930123456"

    await engine.end_call("CA_re")
    await _settle_rows(channel)
    closed = await calls.get("CA_re")
    assert closed["started_at"] == first["started_at"] and closed["ended_at"] is not None
    await channel.stop()


async def test_rows_left_open_by_a_previous_run_are_closed_at_start(tmp_path):
    from pathlib import Path

    from voice_harness.settings import apply_test_paths

    from pincer.services.voice.calls import CallsService

    settings = apply_test_paths(MagicMock(), tmp_path)
    calls = await CallsService.for_path(Path(settings.db_path))
    await calls.save_call({"call_sid": "CA_orphan", "direction": "inbound", "started_at": "2026-10-01T10:00:00+00:00"})
    await calls.save_call(
        {
            "call_sid": "CA_done",
            "direction": "inbound",
            "started_at": "2026-10-01T09:00:00+00:00",
            "ended_at": "2026-10-01T09:05:00+00:00",
            "failure_code": "none",
        }
    )

    channel = VoiceChannel(settings)
    await channel.start(AsyncMock())
    orphan = await calls.get("CA_orphan")
    assert orphan["ended_at"] == orphan["started_at"] and orphan["failure_code"] == "shutdown"
    done = await calls.get("CA_done")
    assert done["ended_at"] == "2026-10-01T09:05:00+00:00" and done["failure_code"] == "none"


async def test_a_dashboard_hangup_mid_action_is_not_a_failure(tmp_path):
    from pincer.voice.engine import ENDED_BY_DASHBOARD, ENDED_BY_KEY
    from pincer.voice.state_machine import CallPhase

    _settings, engine, channel = await _channel_with_fake_engine(tmp_path)
    state = await engine.on_call_start("CA_own", "+4930123456", CallDirection.INBOUND, language="en")
    sm = channel.get_state_machine("CA_own")
    assert sm is not None and sm.phase not in (CallPhase.COMPLETED, CallPhase.FAILED)
    state.metadata[ENDED_BY_KEY] = ENDED_BY_DASHBOARD
    await engine.end_call("CA_own")
    assert sm.phase == CallPhase.COMPLETED
    assert state.metadata["failure_code"] == "none"
    await _settle_rows(channel)
    await channel.stop()
