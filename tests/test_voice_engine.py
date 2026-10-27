"""Tests for the voice engine abstraction layer."""

from __future__ import annotations

import asyncio
import contextlib
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


def _redirect_errors_as_requests_raises_them() -> list[tuple[str, Exception, bool]]:
    """(id, error, may Twilio have applied it) — each built the way the installed
    requests builds it (requests/adapters.py `send`, requests/models.py `iter_content`)."""
    import http.client

    import requests
    from twilio.base.exceptions import TwilioRestException
    from urllib3.exceptions import (
        ConnectTimeoutError,
        MaxRetryError,
        NewConnectionError,
        ProtocolError,
        ReadTimeoutError,
    )

    aborted = ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed without response"))
    return [
        ("read_timeout", requests.exceptions.ReadTimeout(ReadTimeoutError(None, "/", "Read timed out.")), True),
        (
            "body_read_timeout",
            requests.exceptions.ConnectionError(ReadTimeoutError(None, "/", "Read timed out.")),
            True,
        ),
        ("aborted_after_send", requests.exceptions.ConnectionError(aborted), True),
        ("chunked_encoding", requests.exceptions.ChunkedEncodingError(ProtocolError("incomplete read")), True),
        ("connect_timeout", requests.exceptions.ConnectTimeout(MaxRetryError(None, "/", ConnectTimeoutError())), False),
        (
            "refused_or_dns",
            requests.exceptions.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused"))),
            False,
        ),
        ("twilio_error", TwilioRestException(503, "/Calls/CAx"), False),
        ("other", RuntimeError("cannot schedule new futures after shutdown"), False),
    ]


@pytest.mark.parametrize(
    ("error", "may_have_applied"),
    [(e, applied) for _, e, applied in _redirect_errors_as_requests_raises_them()],
    ids=[name for name, _, _ in _redirect_errors_as_requests_raises_them()],
)
def test_redirect_error_classification(error, may_have_applied):
    """Uncertain only when the request may have reached Twilio and its answer
    was lost; definite when it never left (connect timeout, refused, DNS) or
    Twilio answered with an error."""
    from pincer.voice.engine import redirect_may_have_applied

    assert redirect_may_have_applied(error) is may_have_applied


def _local_server(behaviour: str):
    """A local HTTP server misbehaving in one way; returns (url, stop)."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    if behaviour == "refused":
        srv.close()
        return f"http://127.0.0.1:{port}/", lambda: None
    srv.listen(4)
    held: list[socket.socket] = []

    def serve() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        held.append(conn)
        if behaviour == "silent":
            return  # read the request? never answer
        conn.recv(65536)
        if behaviour == "close_after_request":
            conn.close()
        elif behaviour == "stall_body":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")

    threading.Thread(target=serve, daemon=True).start()

    def stop() -> None:
        for conn in held:
            conn.close()
        srv.close()

    return f"http://127.0.0.1:{port}/", stop


@pytest.mark.parametrize(
    ("behaviour", "may_have_applied"),
    [("silent", True), ("close_after_request", True), ("stall_body", True), ("refused", False)],
)
def test_redirect_error_classification_against_real_sockets(behaviour, may_have_applied):
    """The installed twilio + requests + urllib3 stack, end to end through the
    TwilioHttpClient the engine builds, against a local server."""
    from twilio.http.http_client import TwilioHttpClient

    from pincer.voice.engine import redirect_may_have_applied

    url, stop = _local_server(behaviour)
    try:
        with pytest.raises(Exception) as raised:
            TwilioHttpClient(timeout=0.3).request("POST", url, data={"Twiml": "<Response/>"})
    finally:
        stop()
    assert redirect_may_have_applied(raised.value) is may_have_applied, repr(raised.value)


def _definite_redirect_errors() -> list[Exception]:
    import requests
    from twilio.base.exceptions import TwilioException

    return [
        requests.exceptions.ConnectTimeout("connect"),
        requests.exceptions.ConnectionError("dns"),
        TwilioException("no credentials"),
        RuntimeError("cannot schedule new futures after shutdown"),
    ]


@pytest.mark.parametrize("error", _definite_redirect_errors(), ids=lambda e: type(e).__name__)
async def test_transfer_redirect_definite_errors_roll_back(mock_settings, error):
    """Only a read timeout leaves open whether Twilio applied the redirect.
    These never reached Twilio (or never got a request out), so no <Dial> can
    exist: roll back and raise, socket or not."""
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = error
        with pytest.raises(type(error)):
            await engine.transfer_call("CAx", "+4930123")
    assert "transferring" not in state.metadata
    assert "transfer" not in state.metadata


async def test_transfer_dial_twiml_failure_marks_nothing(mock_settings, monkeypatch):
    """A <Dial> that cannot even be built leaves no trace of a transfer."""
    from pincer.voice import engine as engine_module

    def broken(*_args, **_kwargs):
        raise ValueError("bad target")

    monkeypatch.setattr(engine_module, "build_dial_twiml", broken)
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    with patch("twilio.rest.Client") as client_cls, pytest.raises(ValueError, match="bad target"):
        await engine.transfer_call("CAx", "+4930123")
    client_cls.return_value.calls.return_value.update.assert_not_called()
    assert not {"transferring", "transfer", engine_module.DIALS_KEY, engine_module.DIAL_ID_KEY} & set(state.metadata)


def _connected_socket() -> MagicMock:
    from starlette.websockets import WebSocketState

    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    return ws


async def _timed_out_transfer(
    mock_settings, monkeypatch, *, socket=None, twilio_status="in-progress", settle_s=0, error=None
):
    """A transfer whose redirect timed out, settled at once: (engine, state, outcomes, started)."""
    import requests

    from pincer.voice import engine as engine_module
    from pincer.voice.telemetry import hooks

    monkeypatch.setattr(engine_module, "UNCONFIRMED_REDIRECT_SETTLE_S", settle_s)
    monkeypatch.setattr(engine_module, "TRANSFER_CONFIRM_BACKOFF_S", (0.0, 0.0, 0.0))
    started: list[str] = []
    monkeypatch.setattr(hooks, "transfer_started", lambda call_sid, **_: started.append(call_sid))
    engine = ConversationRelayEngine(mock_settings)
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    if socket is not None:
        state.metadata["websocket"] = socket
    outcomes: list[tuple[str, str]] = []

    async def settled(call_sid, outcome):
        outcomes.append((call_sid, outcome))

    engine.set_on_transfer_settled(settled)
    if isinstance(twilio_status, Exception):
        engine.fetch_call_status = AsyncMock(side_effect=twilio_status)
    else:
        engine.fetch_call_status = AsyncMock(return_value=twilio_status)
    with patch("twilio.rest.Client") as client_cls:
        client_cls.return_value.calls.return_value.update.side_effect = error or requests.exceptions.ReadTimeout("read")
        await engine.transfer_call("CAx", "+4930123")  # returns: the turn must end for CR to see the close
    return engine, state, outcomes, started


async def test_transfer_redirect_timeout_unknown_socket_stays_unconfirmed(mock_settings, monkeypatch):
    """No socket was known before the update (the HTTP relay mode): nothing tells
    whether the redirect applied. The call stays in flight, and no transfer is
    recorded as started until a dial result confirms it."""
    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY

    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch)
    record = state.metadata[DIALS_KEY][f"dial:{state.metadata[DIAL_ID_KEY]}"]
    assert record["redirect_unconfirmed"] is True
    await asyncio.wait_for(engine.pending_transfer_settle("CAx"), timeout=5)
    assert outcomes == [("CAx", "unknown")]
    assert state.metadata["transferring"] is True
    assert state.metadata["transfer"] is record
    assert started == []


@pytest.mark.parametrize("name", ["body_read_timeout", "aborted_after_send", "chunked_encoding"])
async def test_transfer_redirect_lost_answer_stays_unconfirmed(mock_settings, monkeypatch, name):
    """requests reports these as ConnectionError/ChunkedEncodingError, yet the
    request reached Twilio: the redirect may be live, so no rollback."""
    error = {n: e for n, e, _ in _redirect_errors_as_requests_raises_them()}[name]
    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch, error=error)
    assert state.metadata["transferring"] is True
    assert state.metadata["transfer"]["redirect_unconfirmed"] is True
    assert engine.pending_transfer_settle("CAx") is not None


async def test_transfer_redirect_timeout_fetch_error_stays_unconfirmed(mock_settings, monkeypatch):
    engine, state, outcomes, started = await _timed_out_transfer(
        mock_settings, monkeypatch, twilio_status=RuntimeError("twilio unreachable")
    )
    await asyncio.wait_for(engine.pending_transfer_settle("CAx"), timeout=5)
    assert engine.fetch_call_status.await_count == 4  # the settle + 3 re-checks, all failing
    assert outcomes == [("CAx", "unknown")]
    assert state.metadata["transferring"] is True
    assert started == []


async def test_transfer_redirect_timeout_with_media_up_is_not_applied(mock_settings, monkeypatch):
    """Once the turn is over, the call's own socket is still open: Twilio did not
    apply the redirect (applying it ends the <Connect>). The channel is told, and
    the call is ours again."""
    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY

    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch, socket=_connected_socket())
    assert state.metadata["transferring"] is True  # unsettled until the turn has returned
    await asyncio.wait_for(engine.pending_transfer_settle("CAx"), timeout=5)
    assert outcomes == [("CAx", "not_applied")]
    assert "transferring" not in state.metadata
    assert state.metadata[DIALS_KEY][f"dial:{state.metadata[DIAL_ID_KEY]}"]["redirect_failed"] is True
    engine.fetch_call_status.assert_not_awaited()
    assert started == []


async def test_transfer_confirm_backoff_is_bounded(mock_settings, monkeypatch, caplog):
    """No evidence and Twilio says in-progress at every re-check: the call is
    left in flight after the last one (the stuck-call reaper bounds it)."""
    from pincer.voice import engine as engine_module

    monkeypatch.setattr(engine_module, "TRANSFER_CONFIRM_BACKOFF_S", (0.0, 0.0, 0.0))
    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch)
    await asyncio.wait_for(engine.pending_transfer_settle("CAx"), timeout=5)
    assert engine.fetch_call_status.await_count == 4  # the settle + 3 re-checks
    assert state.metadata["transferring"] is True
    assert outcomes == [("CAx", "unknown")]
    assert "still unconfirmed" in caplog.text


async def test_possibly_live_dial_defers_only_until_its_deadline(mock_settings, monkeypatch):
    """After a not-applied verdict a closed socket is deferred only while the
    dial could still produce a result; past that the call ends as usual."""
    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch, socket=_connected_socket())
    await asyncio.wait_for(engine.pending_transfer_settle("CAx"), timeout=5)
    assert state.metadata["transfer"]["possibly_live_until"] > 0
    state.metadata["transfer"]["possibly_live_until"] = 0.0  # the dial timeout and margin have passed
    state.metadata.pop("websocket")
    await engine.on_media_closed("CAx")
    assert engine.get_call_state("CAx") is None


async def test_transfer_settle_is_cancelled_when_the_call_ends(mock_settings, monkeypatch):
    """The call ends before the settle fires: the check is dropped, with no
    callback and no error."""
    engine, state, outcomes, started = await _timed_out_transfer(mock_settings, monkeypatch, settle_s=3600)
    task = engine.pending_transfer_settle("CAx")
    assert task is not None
    await engine.finish_remote_ended("CAx", reason="provider_completed")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)  # a settle left running would sleep for an hour
    assert task.cancelled()
    assert engine.pending_transfer_settle("CAx") is None
    assert outcomes == []


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


async def test_media_stream_stt_setup_replaces_the_previous_stream(mock_settings):
    """A new stream socket taking over the call sets up STT again: the old
    Deepgram stream is closed and its consumer stopped, not leaked until the
    provider times it out."""

    class _Stream:
        def __init__(self) -> None:
            self.closed = False

        async def receive_transcripts(self):
            await asyncio.Event().wait()  # a live stream: transcripts until closed
            yield  # pragma: no cover

        async def close(self) -> None:
            self.closed = True

        async def send_audio(self, _chunk) -> None:
            pass

    streams: list[_Stream] = []

    async def start_stream(_config):
        streams.append(_Stream())
        return streams[-1]

    engine = MediaStreamEngine(mock_settings)
    engine._stt_provider = MagicMock(start_stream=AsyncMock(side_effect=start_stream))
    state = await engine.on_call_start("CAx", "+4917", CallDirection.INBOUND)
    await engine.setup_media_stream_stt("CAx", "MS1")
    first_task = state.metadata["stt_consumer_task"]
    await engine.setup_media_stream_stt("CAx", "MS2")
    try:
        assert streams[0].closed is True
        assert first_task.cancelled() or first_task.done()
        assert state.metadata["stt_stream"] is streams[1] and not streams[1].closed
        assert not state.metadata["stt_consumer_task"].done()
    finally:
        await asyncio.wait_for(engine.close_media_stream("CAx"), timeout=5)


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
