"""Tests for the ConversationRelay WebSocket endpoint (hotfix: CR is WS-only)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from voice_harness.strict_async import FAIL_ON_UNAWAITED_COROUTINE

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import scheduling, twiml_server
from pincer.voice.engine import CallDirection, CallState, ConversationRelayEngine, VoiceEngine
from pincer.voice.transcript import Speaker, TranscriptLogger
from pincer.voice.webhook_auth import WS_RELAY_PATH, WS_STREAM_PATH, signed_ws_query, ws_token

pytestmark = FAIL_ON_UNAWAITED_COROUTINE


class FakeEngine:
    def __init__(self) -> None:
        self.states: dict[str, CallState] = {}
        self.speech_inputs: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.heard: list[str | None] = []
        self.started: list[tuple[str, str, CallDirection]] = []
        self.fallbacks: list[str] = []
        self.media_closed: list[str] = []
        self.stt_closed: list[str] = []  # close_media_stream (Media Streams STT)
        self.answered: list[str] = []
        # what VoiceEngine.end_transfer reads
        self._active_calls = self.states
        self._on_transfer_end_callback: Any = None

    async def fallback_and_end(self, call_sid: str) -> None:
        self.fallbacks.append(call_sid)
        self.states.pop(call_sid, None)

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self.states.get(call_sid)

    async def on_call_start(self, call_sid: str, caller: str, direction: CallDirection, **kwargs: Any) -> CallState:
        state = CallState(call_sid=call_sid, direction=direction, caller_number=caller)
        self.states[call_sid] = state
        self.started.append((call_sid, caller, direction))
        return state

    async def mark_call_answered(self, call_sid: str) -> CallState | None:
        """Twilio's `setup` = the callee picked up. Mirrors VoiceEngine: this
        is where per-call tracking starts, and it is idempotent."""
        state = self.states.get(call_sid)
        if state is None or state.metadata.get("answered_at") is not None:
            return state
        state.metadata["answered_at"] = datetime.now(UTC)
        self.answered.append(call_sid)
        return state

    async def on_speech_input(self, call_sid: str, text: str) -> None:
        self.speech_inputs.append((call_sid, text))

    async def on_caller_barge_in(self, call_sid: str, heard: str | None = None) -> None:
        self.interrupts.append(call_sid)
        self.heard.append(heard)

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self.states)

    async def fetch_call_status(self, call_sid: str) -> str:
        return "in-progress"

    # the real implementation, not a copy that can drift from it
    end_transfer = VoiceEngine.end_transfer

    async def close_media_stream(self, call_sid: str) -> None:
        self.stt_closed.append(call_sid)

    async def on_media_closed(self, call_sid: str) -> None:
        state = self.states.get(call_sid)
        if state is None or state.metadata.get("transferring"):
            return
        self.states.pop(call_sid, None)
        self.media_closed.append(call_sid)


TEST_AUTH_TOKEN = "test-twilio-auth-token"


def _ws_settings() -> MagicMock:
    """Settings with a real Twilio auth token, so the T8.1 WebSocket token
    check runs for real instead of no-opping on a missing secret."""
    settings = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = TEST_AUTH_TOKEN
    settings.voice_ws_auth_required = True
    settings.voice_webhook_validate = True
    settings.voice_signature_max_age_s = 300
    settings.voice_webhook_base_url = "https://voice.example.com"
    return settings


def _app_with_engine(engine: Any) -> TestClient:
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    app.include_router(twiml_server.voice_router)
    twiml_server.init_voice_routes(engine, _ws_settings())
    return TestClient(app)


def _signed(path: str, ws_path: str = WS_RELAY_PATH) -> str:
    """`path` with the signed `?t=&s=` token Twilio receives in our TwiML."""
    return path + signed_ws_query(_ws_settings(), ws_path)


def _register(engine: FakeEngine, call_sid: str, caller: str = "+1") -> CallState:
    """Pre-register call state, as the signature-verified /webhook (inbound)
    or the dialer (outbound) does before any TwiML — and hence any socket —
    exists. A setup for a SID with no such state is refused."""
    state = CallState(call_sid=call_sid, direction=CallDirection.INBOUND, caller_number=caller)
    engine.states[call_sid] = state
    return state


class TestRelayWebSocket:
    def test_setup_prompt_interrupt_flow(self):
        engine = FakeEngine()
        _register(engine, "CA_ws", "+15551234567")
        client = _app_with_engine(engine)

        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_ws", "from": "+15551234567"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Hello there", "last": True}))
            ws.send_text(json.dumps({"type": "interrupt"}))
            # give the server loop a chance to process before disconnect
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Second turn"}))

        assert engine.answered == ["CA_ws"]
        assert ("CA_ws", "Hello there") in engine.speech_inputs
        assert ("CA_ws", "Second turn") in engine.speech_inputs
        assert engine.interrupts == ["CA_ws"]
        # disconnect ends the call
        assert engine.media_closed == ["CA_ws"]
        assert "CA_ws" not in engine.states

    def test_relay_disconnect_ends_inbound_call(self):
        """Inbound calls have no status callback unless configured in the Twilio
        console; the relay socket closing must end the call (no lingering 'live')."""
        engine = FakeEngine()
        state = CallState(call_sid="CA_hangup", direction=CallDirection.INBOUND, caller_number="+1")
        engine.states["CA_hangup"] = state
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_hangup", "from": "+1"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "bye", "last": True}))
        assert engine.media_closed == ["CA_hangup"]
        assert "websocket" not in state.metadata

    def test_relay_disconnect_during_transfer_keeps_call(self):
        """<Dial> redirect closes the relay socket but the call continues."""
        engine = FakeEngine()
        state = CallState(call_sid="CA_xfer", direction=CallDirection.INBOUND, caller_number="+1")
        state.metadata["transferring"] = True
        engine.states["CA_xfer"] = state
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_xfer", "from": "+1"}))
            # a relay setup for a known call clears the transfer flag
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi", "last": True}))
        # so this disconnect ends the call like any hangup
        assert engine.media_closed == ["CA_xfer"]

        engine2 = FakeEngine()
        state2 = CallState(call_sid="CA_xfer2", direction=CallDirection.INBOUND, caller_number="+1")
        engine2.states["CA_xfer2"] = state2
        client2 = _app_with_engine(engine2)
        with client2.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_xfer2", "from": "+1"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "please transfer me", "last": True}))
            state2.metadata["transferring"] = True  # set by transfer_call() before <Dial>
        assert engine2.media_closed == []
        assert "CA_xfer2" in engine2.states

    def test_relay_setup_after_failed_transfer_marks_reconnected(self):
        """Twilio executed the reconnect TwiML: from here a replayed dial result
        must not re-issue <Connect>. An ordinary setup (no transfer) adds nothing."""
        engine = FakeEngine()
        back = _register(engine, "CA_back")
        back.metadata["transfer"] = {"dial_call_sid": "CAdial", "dial_status": "busy"}
        plain = _register(engine, "CA_plain")
        client = _app_with_engine(engine)
        for call_sid in ("CA_back", "CA_plain"):
            with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
                ws.send_text(json.dumps({"type": "setup", "callSid": call_sid, "from": "+1"}))
                ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi"}))
        assert back.metadata["transfer"]["reconnected"] is True
        assert "transfer" not in plain.metadata

    def test_stream_start_after_failed_transfer_marks_reconnected(self):
        engine = FakeEngine()
        back = _register(engine, "CA_back")
        back.metadata["transfer"] = {"dial_call_sid": "CAdial", "dial_status": "busy"}
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/stream/CA_back", WS_STREAM_PATH)) as ws:
            ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1"}))
            ws.send_text(json.dumps({"event": "stop"}))
        assert back.metadata["transfer"]["reconnected"] is True

    def test_stream_finally_leaves_a_replacing_socket_alone(self):
        """A stream socket that was replaced (a new one took over the call)
        must not clear the new socket's entry when it closes, nor end the call:
        a transfer would read "media gone" while the caller is still streaming."""
        engine = FakeEngine()
        state = _register(engine, "CA_swap")
        replacing = object()
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/stream/CA_swap", WS_STREAM_PATH)) as ws:
            ws.send_text(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))
            deadline = time.monotonic() + 5
            while not engine.speech_inputs and time.monotonic() < deadline:
                time.sleep(0.01)
            assert engine.speech_inputs, "the stream never attached"
            state.metadata["websocket"] = replacing  # a new socket took over the call
        assert state.metadata.get("websocket") is replacing
        assert engine.media_closed == []
        assert engine.stt_closed == []  # the new socket's STT stays open

    def test_setup_attaches_websocket_to_existing_state(self):
        engine = FakeEngine()
        state = CallState(call_sid="CA_known", direction=CallDirection.INBOUND, caller_number="+1")
        engine.states["CA_known"] = state
        client = _app_with_engine(engine)

        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_known", "from": "+1"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi"}))

        assert engine.started == []  # no duplicate registration
        assert ("CA_known", "hi") in engine.speech_inputs

    def test_legacy_voice_path_alias(self):
        engine = FakeEngine()
        _register(engine, "CA_legacy")
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/voice/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_legacy", "from": "+1"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "legacy"}))
        assert ("CA_legacy", "legacy") in engine.speech_inputs

    def test_repeated_tts_errors_trigger_fallback_and_mark_voice(self):
        """Twilio 64111 ('Error converting tokens to speech') twice → the call
        takes the spoken-apology fallback and the voice is blacklisted so the
        next call builds Google-fallback TwiML — never a silent call."""
        from types import SimpleNamespace

        from pincer.voice import voices

        voices._reset_validation_cache_for_tests()
        engine = FakeEngine()
        _register(engine, "CA_tts_err")
        settings = SimpleNamespace(
            elevenlabs_voice_id="cr-bad-voice",
            elevenlabs_voice_id_en="",
            elevenlabs_voice_id_de="",
            elevenlabs_voice_id_uk="",
        )
        app = FastAPI()
        app.include_router(twiml_server.twilio_router)
        twiml_server.init_voice_routes(engine, settings)
        client = TestClient(app)

        error_msg = {
            "type": "error",
            "description": "Error converting tokens to speech, code: 64111, error: , tokens: Hello there.",
        }
        try:
            with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
                ws.send_text(json.dumps({"type": "setup", "callSid": "CA_tts_err", "from": "+1"}))
                ws.send_text(json.dumps(error_msg))
                ws.send_text(json.dumps(error_msg))
                ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "flush"}))

            assert engine.fallbacks == ["CA_tts_err"]
            assert voices.is_voice_invalid("cr-bad-voice", settings)
        finally:
            voices._reset_validation_cache_for_tests()

    def test_single_tts_error_does_not_end_call(self):
        engine = FakeEngine()
        _register(engine, "CA_one_err")
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_one_err", "from": "+1"}))
            ws.send_text(json.dumps({"type": "error", "description": "Error converting tokens to speech, code: 64111"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still going"}))
        assert engine.fallbacks == []
        assert ("CA_one_err", "still going") in engine.speech_inputs

    def test_garbage_messages_ignored(self):
        engine = FakeEngine()
        _register(engine, "CA_g")
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text("not json at all")
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_g", "from": "+1"}))
            ws.send_text(json.dumps({"type": "dtmf", "digit": "1"}))  # unknown type: ignored
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still alive"}))
        assert ("CA_g", "still alive") in engine.speech_inputs


class TestRelaySetupHardening:
    """A socket may only join a call that /webhook or the dialer registered:
    an unknown SID must not mint conversation state under a socket-supplied
    caller identity, and a live call's socket must not be overwritten."""

    def test_setup_with_unknown_sid_is_refused(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_spoof", "from": "+15550001111"}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 1011
        assert engine.started == []
        assert engine.states == {}
        assert engine.speech_inputs == []

    def test_reconnect_after_lost_transfer_state_is_accepted(self, monkeypatch):
        """State lost while the target rang: the signed transfer-result callback
        re-registers the call, so the reconnected relay `setup` is not refused
        as an unknown SID and the caller can leave a message. Only the HTTP
        signature is stubbed; the WS token check runs for real."""

        async def _verified(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(twiml_server, "verify_http_request", _verified)
        monkeypatch.setattr("pincer.voice.receptionist.profile.receptionist_active", lambda _s: False)
        monkeypatch.setattr("pincer.voice.language.resolve_call_language", lambda *_a, **_k: "en")
        monkeypatch.setattr(twiml_server, "_transfer_session_lookup", lambda _sid: None)
        engine = FakeEngine()
        client = _app_with_engine(engine)
        result = client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": "CA_lost", "From": "+15550001111", "DialCallStatus": "busy", "CallStatus": "in-progress"},
        )
        assert "ConversationRelay" in result.text
        assert engine.started == [("CA_lost", "+15550001111", CallDirection.INBOUND)]
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_lost", "from": "+15550001111"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Please call me back", "last": True}))
        assert engine.answered == ["CA_lost"]
        assert ("CA_lost", "Please call me back") in engine.speech_inputs

    def test_stream_with_unknown_sid_is_refused(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with (
            client.websocket_connect(_signed("/api/apps/twilio/stream/CA_spoof", WS_STREAM_PATH)) as ws,
            pytest.raises(WebSocketDisconnect) as exc,
        ):
            ws.receive_text()
        assert exc.value.code == 1008
        assert engine.started == []
        assert engine.states == {}

    def test_second_socket_for_live_call_is_refused(self):
        """With a live socket attached, a setup for the same SID must not
        steal the call — overwriting `metadata['websocket']` would redirect
        the agent's speech and leave the genuine caller silent."""
        engine = FakeEngine()
        state = _register(engine, "CA_live")
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws1:
            ws1.send_text(json.dumps({"type": "setup", "callSid": "CA_live", "from": "+1"}))
            deadline = time.time() + 2
            while "websocket" not in state.metadata and time.time() < deadline:
                time.sleep(0.01)
            assert "websocket" in state.metadata
            owner = state.metadata["websocket"]

            with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws2:
                ws2.send_text(json.dumps({"type": "setup", "callSid": "CA_live", "from": "+6660001111"}))
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws2.receive_text()
            assert exc.value.code == 1008
            assert state.metadata["websocket"] is owner  # the call still belongs to its socket

            ws1.send_text(json.dumps({"type": "prompt", "voicePrompt": "still mine"}))
        assert ("CA_live", "still mine") in engine.speech_inputs


class TestRelayWebSocketAuth:
    """T8.1: an unsigned or forged WS upgrade is refused BEFORE accept().

    This is the Hotfix-2 debug step ("just point wscat at the relay") turned
    into a security test — that shortcut must no longer work.
    """

    def test_unsigned_connect_is_refused(self):
        client = _app_with_engine(FakeEngine())
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/apps/twilio/relay"):
            pass  # pragma: no cover — the handshake never completes
        assert exc.value.code == 1008

    def test_unsigned_legacy_alias_is_refused(self):
        client = _app_with_engine(FakeEngine())
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/relay"):
            pass  # pragma: no cover

    def test_unsigned_media_stream_is_refused(self):
        client = _app_with_engine(FakeEngine())
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/apps/twilio/stream/CA_x"):
            pass  # pragma: no cover

    def test_forged_token_is_refused(self):
        client = _app_with_engine(FakeEngine())
        forged = "/api/apps/twilio/relay?t=99999999999&s=not-a-real-signature"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(forged):
            pass  # pragma: no cover

    def test_token_signed_for_another_path_is_refused(self):
        """A stream token must not open the relay socket."""
        client = _app_with_engine(FakeEngine())
        stream_query = signed_ws_query(_ws_settings(), WS_STREAM_PATH)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/api/apps/twilio/relay{stream_query}"):
            pass  # pragma: no cover

    def test_stale_token_is_refused(self):
        """Replay guard: a token minted outside the signature window is dead."""
        client = _app_with_engine(FakeEngine())
        stale_ts = int(time.time()) - 4000
        token = ws_token(TEST_AUTH_TOKEN, WS_RELAY_PATH, stale_ts)
        stale = f"/api/apps/twilio/relay?t={stale_ts}&s={token}"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(stale):
            pass  # pragma: no cover

    def test_signed_media_stream_is_accepted(self):
        engine = FakeEngine()
        _register(engine, "CA_sig")
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/stream/CA_sig", WS_STREAM_PATH)) as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(json.dumps({"event": "stop"}))
        assert engine.stt_closed == ["CA_sig"]  # its own close does close the STT

    def test_start_media_format_is_known_before_stt_setup(self):
        """setup_media_stream_stt checks Twilio's declared format; it has to
        be on the state by the time STT is configured."""
        seen: list[Any] = []

        class StreamEngine(FakeEngine):
            async def setup_media_stream_stt(self, call_sid: str, stream_sid: str) -> None:
                seen.append(self.states[call_sid].metadata.get("media_format"))

        engine = StreamEngine()
        _register(engine, "CA_fmt")
        client = _app_with_engine(engine)
        media_format = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
        with client.websocket_connect(_signed("/api/apps/twilio/stream/CA_fmt", WS_STREAM_PATH)) as ws:
            ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {"mediaFormat": media_format}}))
            ws.send_text(json.dumps({"event": "stop"}))

        assert seen == [media_format]

    def test_auth_disabled_allows_unsigned_connect(self):
        """Dev escape hatch still works — and doctor reports it CRITICAL."""
        engine = FakeEngine()
        _register(engine, "CA_open", "+15551234567")
        settings = _ws_settings()
        settings.voice_ws_auth_required = False
        app = FastAPI()
        app.include_router(twiml_server.twilio_router)
        twiml_server.init_voice_routes(engine, settings)
        client = TestClient(app)
        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_open", "from": "+15551234567"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi"}))
        assert engine.answered == ["CA_open"]
        assert ("CA_open", "hi") in engine.speech_inputs


# ── Barge-in through the real read loop ─────────────────────────────────────
#
# The FakeEngine above returns from on_speech_input at once, so it can never
# show the socket loop blocked. These drive the production stack —
# relay_ws → ConversationRelayEngine → VoiceChannel — with a scripted LLM that
# stalls mid-reply, in the test's own event loop (TestClient runs the app on
# another thread, where the stall and the interrupt could not be sequenced
# without sleeping).

OLD_FIRST = "Der erste Satz der alten Antwort."
OLD_NEVER = "Dieser Satz darf niemals gesprochen werden."
NEW_REPLY = "Die neue Antwort auf die Unterbrechung."


class _Trace:
    """Turn trace stand-in: records the cancellation, ignores the stamps."""

    def __init__(self) -> None:
        self.cancelled_with: str | None = None

    def cancel(self, reason: str = "") -> None:
        self.cancelled_with = self.cancelled_with or reason

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None


@pytest.fixture
def relay_call(monkeypatch):
    from test_voice_streaming_turn import FakeStreamAgent, _settings
    from voice_harness.transport_fakes import FakeSocket, FakeTwilio, install_fake_twilio

    scheduling._reset_for_tests()
    install_fake_twilio(monkeypatch, FakeTwilio)  # real engine: ending a call would hit Twilio

    async def _authenticated(*_args) -> bool:
        return True

    async def _start(scripts):
        settings = _settings()
        engine = ConversationRelayEngine(settings)
        channel = VoiceChannel(settings)
        channel.set_engine(engine)
        agent = FakeStreamAgent(scripts)
        channel.set_stream_agent(agent)
        traces: list[_Trace] = []

        def _open_trace(call_sid, _state, _arrival_ns, _markers=None):
            traces.append(_Trace())
            channel._turn_traces[call_sid] = traces[-1]
            return traces[-1]

        channel._open_turn_trace = _open_trace

        async def _handler(_incoming):
            return ""

        await channel.start(_handler)
        monkeypatch.setattr(twiml_server, "_engine", engine)
        monkeypatch.setattr(twiml_server, "_settings", settings)
        monkeypatch.setattr(twiml_server, "_authenticate_ws", _authenticated)
        await engine.on_call_start("CA_cr", "+4930123456", CallDirection.INBOUND, language="de")
        sock = FakeSocket()
        loop = asyncio.create_task(twiml_server.relay_ws(sock))
        sock.push(type="setup", callSid="CA_cr", **{"from": "+4930123456"})
        return engine, channel, agent, sock, loop, traces

    yield _start
    scheduling._reset_for_tests()


def _scripts():
    from test_voice_streaming_turn import text_chunks

    return [text_chunks(f"{OLD_FIRST} ", OLD_NEVER), text_chunks(NEW_REPLY)]


class TestRelayBargeIn:
    # regression: fails on dev @ 0a1c320 (the loop never reads the interrupt)
    async def test_interrupt_is_read_and_cancels_the_paused_turn(self, relay_call):
        engine, channel, agent, sock, loop, traces = await relay_call(_scripts())
        agent.pause_after = 1
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        assert sock.tokens() == [OLD_FIRST]

        # The turn is still running. The read loop must get to this anyway.
        agent.pause_after = None
        sock.push(type="interrupt", utteranceUntilInterrupt="Der erste Satz", durationUntilInterruptMs=900)
        await asyncio.wait_for(sock.inbox.join(), timeout=2)
        assert traces[0].cancelled_with == "barge_in"  # before the LLM was ever resumed
        assert "CA_cr" not in channel._active_turns

        sock.push(type="prompt", voicePrompt="Zweite Frage", last=True)
        await asyncio.wait_for(sock.inbox.join(), timeout=2)
        await channel.wait_for_turn("CA_cr")

        assert sock.tokens() == [OLD_FIRST, NEW_REPLY]
        transcript = channel.get_transcript("CA_cr")
        old_line = next(e for e in transcript.entries if e.speaker == Speaker.AGENT and e.text == OLD_FIRST)
        assert old_line.state == "interrupted"

        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await channel.stop()

    async def test_an_interrupt_over_the_http_webhook_cancels_the_paused_turn(self, relay_call):
        """The relay's HTTP transport gets the same barge-in as its socket."""
        import httpx
        from fastapi import FastAPI

        engine, channel, agent, sock, loop, traces = await relay_call(_scripts())
        engine._settings.voice_webhook_validate = False
        agent.pause_after = 1
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        agent.pause_after = None

        app = FastAPI()
        app.include_router(twiml_server.twilio_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/apps/twilio/relay-webhook",
                json={"type": "interrupt", "callSid": "CA_cr", "utteranceUntilInterrupt": "Der erste Satz"},
            )
        assert response.status_code == 200

        assert traces[0].cancelled_with == "barge_in"
        assert "CA_cr" not in channel._active_turns
        old_line = next(e for e in channel.get_transcript("CA_cr").entries if e.text == OLD_FIRST)
        assert old_line.state == "interrupted"
        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await channel.stop()

    async def test_the_closing_line_of_a_reply_ends_its_utterance(self, relay_call):
        engine, channel, agent, sock, loop, _traces = await relay_call([_scripts()[1]])
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(sock.inbox.join(), timeout=2)
        await channel.wait_for_turn("CA_cr")

        line = next(e for e in channel.get_transcript("CA_cr").entries if e.text == NEW_REPLY)
        assert line.ends_utterance
        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await channel.stop()

    async def test_a_failed_turn_closes_its_utterance_before_the_retry_line(self, relay_call):
        """s1 and s2 streamed, then the LLM dies: the closer ends their
        utterance and the retry line is its own. A barge-in that names the
        retry line must leave s1 and s2, which played in full, alone."""
        from test_voice_streaming_turn import text_chunks

        from pincer.voice.prompts import get_prompt

        second = "Der zweite Satz ist auch noch lang genug."
        script = text_chunks(f"{OLD_FIRST} ", f"{second} ", "Dieser dritte wird nie gesendet. ")
        engine, channel, agent, sock, loop, _traces = await relay_call([script])
        agent.fail_after = 2  # two sentences out (last=False), then the stream raises
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(sock.inbox.join(), timeout=2)
        await channel.wait_for_turn("CA_cr")

        retry_line = str(get_prompt("BRAIN_ERROR_RETRY", "de", "sie"))
        texts = [(m["token"], m["last"]) for m in sock.sent if m.get("type") == "text"]
        assert texts == [(OLD_FIRST, False), (second, False), ("", True), (retry_line, True)]
        transcript = channel.get_transcript("CA_cr")
        assert [(e.text, e.ends_utterance) for e in transcript.entries if e.speaker == Speaker.AGENT] == [
            (OLD_FIRST, False),
            (second, True),
            (retry_line, True),
        ]

        played = _states(transcript)[:2]  # the two streamed sentences, as the turn recorded them
        transcript.mark_interrupted(retry_line.split()[0])
        assert _states(transcript) == [*played, "interrupted"]
        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await channel.stop()

    async def test_shutdown_closes_the_open_utterance_before_the_goodbye(self, relay_call):
        """The cancelled turn closes its CR utterance; the goodbye never continues it."""
        engine, channel, agent, sock, loop, _traces = await relay_call(_scripts())
        agent.pause_after = 1
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        assert [(m["token"], m["last"]) for m in sock.sent if m.get("type") == "text"] == [(OLD_FIRST, False)]

        await channel.stop()

        texts = [(m["token"], m["last"]) for m in sock.sent if m.get("type") == "text"]
        assert texts[:2] == [(OLD_FIRST, False), ("", True)]  # closed before the goodbye
        assert texts[2][0] and texts[2][1]  # the goodbye is its own utterance
        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)

    # regression: fails on dev @ 0a1c320
    async def test_interrupt_and_the_next_prompt_count_once(self, relay_call):
        engine, channel, agent, sock, loop, _traces = await relay_call(_scripts())
        counted: list[str] = []
        original = engine.interrupt_speech

        async def _counting(call_sid: str) -> None:
            counted.append(call_sid)
            await original(call_sid)

        engine.interrupt_speech = _counting
        agent.pause_after = 1
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        agent.pause_after = None
        sock.push(type="interrupt", utteranceUntilInterrupt="")
        sock.push(type="prompt", voicePrompt="Moment!", last=True)
        await asyncio.wait_for(sock.inbox.join(), timeout=2)
        await channel.wait_for_turn("CA_cr")

        assert counted == ["CA_cr"]
        assert sock.tokens()[-1] == NEW_REPLY
        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await channel.stop()

    # regression: fails on dev @ 0a1c320 (the hangup is never read mid-turn)
    async def test_hangup_mid_reply_stops_the_turn(self, relay_call, caplog):
        engine, channel, agent, sock, loop, traces = await relay_call(_scripts())
        agent.pause_after = 1
        sock.push(type="prompt", voicePrompt="Erste Frage?", last=True)
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        turn = channel._active_turns["CA_cr"]

        sock.hang_up()
        await asyncio.wait_for(loop, timeout=2)
        await asyncio.wait_for(asyncio.wait([turn]), timeout=2)

        assert turn.cancelled()
        assert traces[0].cancelled_with == "call_ended"
        assert not channel._active_turns and not channel._utterance_tasks
        assert OLD_NEVER not in sock.tokens()
        assert "DROPPED" not in caplog.text
        await channel.stop()


def _reply(*utterances: list[str]) -> TranscriptLogger:
    """A caller question, then the agent's reply as provider utterances."""
    transcript = TranscriptLogger("CA_t")
    transcript.log_utterance(Speaker.CALLER, "Wann ist mein Termin?")
    for lines in utterances:
        for line in lines:
            transcript.log_utterance(Speaker.AGENT, line, state="freeform")
        transcript.end_agent_utterance()
    return transcript


def _states(transcript: TranscriptLogger) -> list[str]:
    return [e.state for e in transcript.entries if e.speaker == Speaker.AGENT]


class TestMarkInterrupted:
    def test_an_earlier_utterance_counts_as_heard(self):
        """Filler played in full; `heard` covers only the answer being played."""
        transcript = _reply(["Einen Moment bitte."], ["Der Termin ist am Montag.", "Um zehn Uhr."])

        marked = transcript.mark_interrupted("Der Termin ist am")

        assert _states(transcript) == ["freeform", "interrupted", "undelivered"]
        assert marked == 2

    def test_heard_lines_of_the_playing_utterance_keep_their_state(self):
        transcript = _reply(["Einen Moment bitte."], ["Der Termin ist am Montag.", "Um zehn Uhr.", "Passt das?"])

        transcript.mark_interrupted("Der Termin ist am Montag. Um zehn")

        assert _states(transcript) == ["freeform", "freeform", "interrupted", "undelivered"]

    def test_utterances_after_the_playing_one_never_played(self):
        transcript = _reply(["Einen Moment bitte."], ["Der Termin ist am Montag."])

        transcript.mark_interrupted("Einen Moment")

        assert _states(transcript) == ["interrupted", "undelivered"]

    def test_unmatched_heard_marks_only_the_latest_utterance(self):
        transcript = _reply(["Einen Moment bitte."], ["Der Termin ist am Montag."])

        transcript.mark_interrupted("")

        assert _states(transcript) == ["freeform", "interrupted"]

    def test_the_latest_utterance_starting_with_heard_is_the_one_playing(self):
        """A short earlier line is easily a prefix of the line really playing."""
        transcript = _reply(["Ja."], ["Ja, der Termin ist am Montag."])

        transcript.mark_interrupted("Ja")

        assert _states(transcript) == ["freeform", "interrupted"]

    def test_a_single_open_utterance_is_unchanged(self):
        transcript = TranscriptLogger("CA_t")
        transcript.log_utterance(Speaker.CALLER, "Hallo?")
        transcript.log_utterance(Speaker.AGENT, "Der erste Satz.", state="freeform")
        transcript.log_utterance(Speaker.AGENT, "Der zweite Satz.", state="freeform")

        transcript.mark_interrupted("Der erste Satz. Der zw")

        assert _states(transcript) == ["freeform", "interrupted"]
