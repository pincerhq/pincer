"""Tests for the ConversationRelay WebSocket endpoint (hotfix: CR is WS-only)."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pincer.voice import twiml_server
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.webhook_auth import WS_RELAY_PATH, WS_STREAM_PATH, signed_ws_query, ws_token


class FakeEngine:
    def __init__(self) -> None:
        self.states: dict[str, CallState] = {}
        self.speech_inputs: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.started: list[tuple[str, str, CallDirection]] = []
        self.fallbacks: list[str] = []
        self.media_closed: list[str] = []

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

    async def on_speech_input(self, call_sid: str, text: str) -> None:
        self.speech_inputs.append((call_sid, text))

    async def interrupt_speech(self, call_sid: str) -> None:
        self.interrupts.append(call_sid)

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self.states)

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


class TestRelayWebSocket:
    def test_setup_prompt_interrupt_flow(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)

        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_ws", "from": "+15551234567"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Hello there", "last": True}))
            ws.send_text(json.dumps({"type": "interrupt"}))
            # give the server loop a chance to process before disconnect
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Second turn"}))

        assert engine.started and engine.started[0][0] == "CA_ws"
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
            assert voices.is_voice_invalid("cr-bad-voice")
        finally:
            voices._reset_validation_cache_for_tests()

    def test_single_tts_error_does_not_end_call(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_one_err", "from": "+1"}))
            ws.send_text(json.dumps({"type": "error", "description": "Error converting tokens to speech, code: 64111"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still going"}))
        assert engine.fallbacks == []
        assert ("CA_one_err", "still going") in engine.speech_inputs

    def test_garbage_messages_ignored(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/relay")) as ws:
            ws.send_text("not json at all")
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_g", "from": "+1"}))
            ws.send_text(json.dumps({"type": "dtmf", "digit": "1"}))  # unknown type: ignored
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still alive"}))
        assert ("CA_g", "still alive") in engine.speech_inputs


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
        client = _app_with_engine(engine)
        with client.websocket_connect(_signed("/api/apps/twilio/stream/CA_sig", WS_STREAM_PATH)) as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(json.dumps({"event": "stop"}))

    def test_auth_disabled_allows_unsigned_connect(self):
        """Dev escape hatch still works — and doctor reports it CRITICAL."""
        engine = FakeEngine()
        settings = _ws_settings()
        settings.voice_ws_auth_required = False
        app = FastAPI()
        app.include_router(twiml_server.twilio_router)
        twiml_server.init_voice_routes(engine, settings)
        client = TestClient(app)
        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_open", "from": "+15551234567"}))
        assert engine.started and engine.started[0][0] == "CA_open"
