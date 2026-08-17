"""Tests for the ConversationRelay WebSocket endpoint (hotfix: CR is WS-only)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.voice import twiml_server
from pincer.voice.engine import CallDirection, CallState


class FakeEngine:
    def __init__(self) -> None:
        self.states: dict[str, CallState] = {}
        self.speech_inputs: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.started: list[tuple[str, str, CallDirection]] = []
        self.fallbacks: list[str] = []

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


def _app_with_engine(engine: Any) -> TestClient:
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    app.include_router(twiml_server.voice_router)
    twiml_server.init_voice_routes(engine, MagicMock())
    return TestClient(app)


class TestRelayWebSocket:
    def test_setup_prompt_interrupt_flow(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)

        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_ws", "from": "+15551234567"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Hello there", "last": True}))
            ws.send_text(json.dumps({"type": "interrupt"}))
            # give the server loop a chance to process before disconnect
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Second turn"}))

        assert engine.started and engine.started[0][0] == "CA_ws"
        assert ("CA_ws", "Hello there") in engine.speech_inputs
        assert ("CA_ws", "Second turn") in engine.speech_inputs
        assert engine.interrupts == ["CA_ws"]
        # websocket handle attached for send_speech, then cleaned up on disconnect
        assert "websocket" not in engine.states["CA_ws"].metadata

    def test_setup_attaches_websocket_to_existing_state(self):
        engine = FakeEngine()
        state = CallState(call_sid="CA_known", direction=CallDirection.INBOUND, caller_number="+1")
        engine.states["CA_known"] = state
        client = _app_with_engine(engine)

        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_known", "from": "+1"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "hi"}))

        assert engine.started == []  # no duplicate registration
        assert ("CA_known", "hi") in engine.speech_inputs

    def test_legacy_voice_path_alias(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with client.websocket_connect("/voice/relay") as ws:
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
            with client.websocket_connect("/api/apps/twilio/relay") as ws:
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
        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_one_err", "from": "+1"}))
            ws.send_text(json.dumps({"type": "error", "description": "Error converting tokens to speech, code: 64111"}))
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still going"}))
        assert engine.fallbacks == []
        assert ("CA_one_err", "still going") in engine.speech_inputs

    def test_garbage_messages_ignored(self):
        engine = FakeEngine()
        client = _app_with_engine(engine)
        with client.websocket_connect("/api/apps/twilio/relay") as ws:
            ws.send_text("not json at all")
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA_g", "from": "+1"}))
            ws.send_text(json.dumps({"type": "dtmf", "digit": "1"}))  # unknown type: ignored
            ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "still alive"}))
        assert ("CA_g", "still alive") in engine.speech_inputs
