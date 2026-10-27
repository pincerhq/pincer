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
    dial_id = state.metadata["dial_id"]
    assert (
        'timeLimit="3600"' in kwargs["twiml"] and f'action="https://example.com/tr?dial={dial_id}"' in kwargs["twiml"]
    )
    assert kwargs["status_callback"] == "https://example.com/api/apps/twilio/status"
    assert kwargs["status_callback_method"] == "POST"
    assert state.metadata["transferring"] is True
    assert state.metadata["transfer"]["target"] == "+4930123"
    assert state.metadata["transfer"]["dial_id"] == dial_id


@pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
async def test_each_dial_gets_its_own_token(mock_settings, engine_cls):
    """The token names the dial its result belongs to, and survives a redirect
    that raised (Twilio may have applied it anyway)."""
    from twilio.base.exceptions import TwilioRestException

    engine = engine_cls(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        await engine.transfer_call("CAx", "+4930123", action_url="https://example.com/tr?x=1")
        first = state.metadata["dial_id"]
        twiml = client_cls.return_value.calls.return_value.update.call_args.kwargs["twiml"]
        assert f'action="https://example.com/tr?x=1&amp;dial={first}"' in twiml
        # a definite Twilio error rolls the call back; a timeout with the media
        # gone would keep it in flight (see the redirect-timeout tests)
        client_cls.return_value.calls.return_value.update.side_effect = TwilioRestException(502, "/Calls/CAx")
        with pytest.raises(TwilioRestException):
            await engine.transfer_call("CAx", "+4930123", action_url="https://example.com/tr")
    assert state.metadata["dial_id"] not in ("", first)
    assert "transferring" not in state.metadata and "transfer" not in state.metadata


async def test_transfer_call_redirect_runs_off_the_event_loop(mock_settings):
    """The Twilio REST call blocks for 1–3 s; on the loop it stalls every other call's media."""
    import threading

    engine = ConversationRelayEngine(mock_settings)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    threads: list[int] = []
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = lambda **_: threads.append(
            threading.get_ident()
        )
        await engine.transfer_call("CAx", "+4930123")
    assert threads and threads[0] != threading.get_ident()


async def test_end_transfer_notifies_channel_only_when_a_transfer_ends(mock_settings):
    """The channel moves the phase out of TRANSFERRING on this callback; a relay
    `setup` on an ordinary call (no transfer) must not trigger it."""
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    ended = AsyncMock()
    engine.set_on_transfer_end(ended)
    await engine.end_transfer("CAx")
    ended.assert_not_awaited()
    state.metadata["transferring"] = True
    await engine.end_transfer("CAx")
    await engine.end_transfer("CAx")  # the reconnected relay `setup` repeats it
    ended.assert_awaited_once_with("CAx")
    assert "transferring" not in state.metadata


@pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
@pytest.mark.parametrize("action", ["end_call", "send_dtmf", "fallback_and_end"])
async def test_twilio_updates_run_off_the_event_loop(mock_settings, engine_cls, action):
    """Every Twilio REST update (hangup, DTMF, TTS fallback), not only the transfer."""
    import threading

    engine = engine_cls(mock_settings)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    threads: list[int] = []
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = lambda **_: threads.append(
            threading.get_ident()
        )
        if action == "send_dtmf":
            await engine.send_dtmf("CAx", "1")
        else:
            await getattr(engine, action)("CAx")
    assert threads and all(t != threading.get_ident() for t in threads)


@pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
@pytest.mark.parametrize("action", ["end_call", "send_dtmf", "fallback_and_end", "transfer_call"])
async def test_twilio_updates_bound_the_request(mock_settings, engine_cls, action):
    """The SDK's default is no timeout: a hung Twilio connection would hold a
    worker thread forever, and enough of them stall every other to_thread user."""
    from pincer.voice.engine import TWILIO_UPDATE_TIMEOUT_S

    engine = engine_cls(mock_settings)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        if action == "send_dtmf":
            await engine.send_dtmf("CAx", "1")
        elif action == "transfer_call":
            await engine.transfer_call("CAx", "+4930123")
        else:
            await getattr(engine, action)("CAx")
    assert client_cls.call_args.kwargs["http_client"].timeout == TWILIO_UPDATE_TIMEOUT_S == 10.0


@pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
async def test_fetch_call_status_runs_off_the_event_loop(mock_settings, engine_cls):
    """The status check sits on a webhook's response path: blocking Twilio HTTP
    must not run on the loop, and the status comes back as Twilio spells it."""
    import threading

    engine = engine_cls(mock_settings)
    threads: list[int] = []

    def fetch():
        threads.append(threading.get_ident())
        return MagicMock(status="in-progress")

    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.fetch.side_effect = fetch
        assert await engine.fetch_call_status("CAx") == "in-progress"
    client_cls.return_value.calls.assert_called_once_with("CAx")
    assert threads and threads[0] != threading.get_ident()


async def test_fetch_call_status_bounds_the_request(mock_settings):
    """Twilio waits ~15 s for the webhook that asks; the SDK's own default is no timeout."""
    from pincer.voice.engine import CALL_STATUS_FETCH_TIMEOUT_S

    engine = ConversationRelayEngine(mock_settings)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.fetch.return_value = MagicMock(status="completed")
        assert await engine.fetch_call_status("CAx") == "completed"
    assert client_cls.call_args.kwargs["http_client"].timeout == CALL_STATUS_FETCH_TIMEOUT_S == 5.0


async def test_transfer_call_without_http_base_has_no_status_callback(mock_settings):
    mock_settings.voice_webhook_base_url = ""
    engine = ConversationRelayEngine(mock_settings)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        await engine.transfer_call("CAx", "+4930123")
    kwargs = client_cls.return_value.calls.return_value.update.call_args.kwargs
    assert set(kwargs) == {"twiml"}


async def test_transfer_call_failure_leaves_call_ours(mock_settings):
    """Twilio answered with an error: the redirect definitely did not happen."""
    from twilio.base.exceptions import TwilioRestException

    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = TwilioRestException(503, "/Calls/CAx")
        with pytest.raises(TwilioRestException):
            await engine.transfer_call("CAx", "+4930123")
    assert "transferring" not in state.metadata


async def test_transfer_redirect_timeout_without_media_keeps_the_transfer_in_flight(mock_settings):
    """The update timed out and the media socket is gone: applying the redirect
    is what closes it, so the call is in the <Dial>. Rolling back would put a
    bridged call back on the AI's timers; /transfer-result or /status decides."""
    import requests

    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY

    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = requests.exceptions.ReadTimeout("read")
        await engine.transfer_call("CAx", "+4930123")
    assert state.metadata["transferring"] is True
    record = state.metadata[DIALS_KEY][f"dial:{state.metadata[DIAL_ID_KEY]}"]
    assert record["redirect_unconfirmed"] is True
    assert state.metadata["transfer"] is record


async def test_transfer_redirect_timeout_with_media_up_rolls_back(mock_settings):
    """The update timed out but the media socket is still open: Twilio did not
    apply the redirect, so the call is still ours."""
    import requests

    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    state.metadata["websocket"] = MagicMock()
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = requests.exceptions.ReadTimeout("read")
        with pytest.raises(requests.exceptions.ReadTimeout):
            await engine.transfer_call("CAx", "+4930123")
    assert "transferring" not in state.metadata


async def test_transfer_call_refuses_a_connection_override_fragment(mock_settings):
    """`#rp=…`/`#rt=…` on the action URL would let Twilio execute a copy of a
    dial result after it executed the first (the replay answers assume it
    never does), and the dial token appended after a fragment never reaches us."""
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls, pytest.raises(ValueError, match="connection override"):
        await engine.transfer_call(
            "CAx", "+4930123", action_url="https://example.com/api/apps/twilio/transfer-result#rp=all"
        )
    client_cls.return_value.calls.return_value.update.assert_not_called()
    assert "transferring" not in state.metadata


async def test_redirect_failure_keeps_the_dial_record(mock_settings):
    """The redirect can raise after Twilio applied it, and then this dial's
    result still arrives: its record stays, marked, while the call is ours again."""
    from twilio.base.exceptions import TwilioRestException

    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY

    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = TwilioRestException(500, "/Calls/CAx")
        with pytest.raises(TwilioRestException):
            await engine.transfer_call("CAx", "+4930123")
    record = state.metadata[DIALS_KEY][f"dial:{state.metadata[DIAL_ID_KEY]}"]
    assert record["redirect_failed"] is True
    assert record["target"] == "+4930123"
    assert "transfer" not in state.metadata


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


async def test_fallback_and_end_says_goodbye_then_finalizes_once(mock_settings):
    """TTS fallback: one <Say>+<Hangup> update, then the shared teardown (no REST hangup)."""
    engine = ConversationRelayEngine(mock_settings)
    on_end = AsyncMock()
    engine.set_on_call_end(on_end)
    await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls, patch.object(engine, "end_call") as end_call:
        await engine.fallback_and_end("CAx")
        await engine.fallback_and_end("CAx")  # already finalized: no-op
    update = client_cls.return_value.calls.return_value.update
    update.assert_called_once()
    assert "<Say" in update.call_args.kwargs["twiml"] and "<Hangup/>" in update.call_args.kwargs["twiml"]
    end_call.assert_not_called()
    on_end.assert_awaited_once()
    assert engine.get_call_state("CAx") is None
