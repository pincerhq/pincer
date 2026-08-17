"""Tests for the ElevenLabs streaming TTS upgrade (Sprint 4, T4.2)."""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest

from pincer.voice.tts import (
    OUTPUT_PCM_16000,
    OUTPUT_ULAW_8000,
    ElevenLabsTTS,
    TTSSynthesisError,
)


class FakeWS:
    """Minimal stand-in for a websockets client connection."""

    def __init__(self, incoming: list, fail_on_recv: bool = False):
        self.sent: list[str] = []
        self.closed = False
        self._incoming = list(incoming)
        self._fail_on_recv = fail_on_recv

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_on_recv:
            raise RuntimeError("connection lost")
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


def _install_fake_websockets(monkeypatch, ws: FakeWS, capture: dict):
    module = types.ModuleType("websockets")

    async def connect(url, **kwargs):
        capture["url"] = url
        capture["kwargs"] = kwargs
        return ws

    module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", module)


def _audio_msg(data: bytes) -> str:
    return json.dumps({"audio": base64.b64encode(data).decode()})


class TestUlawPath:
    async def test_requests_ulaw_and_yields_raw_chunks(self, monkeypatch):
        ws = FakeWS([_audio_msg(b"chunk1"), _audio_msg(b"chunk2"), json.dumps({"isFinal": True})])
        capture: dict = {}
        _install_fake_websockets(monkeypatch, ws, capture)

        tts = ElevenLabsTTS(api_key="k", voice_id="v1")
        chunks = [c async for c in tts.synthesize_stream("Hello there.")]

        assert chunks == [b"chunk1", b"chunk2"]
        assert "output_format=ulaw_8000" in capture["url"]
        assert "model_id=eleven_flash_v2_5" in capture["url"]
        assert tts.output_format == OUTPUT_ULAW_8000

    async def test_pcm_fallback_flag(self, monkeypatch):
        ws = FakeWS([json.dumps({"isFinal": True})])
        capture: dict = {}
        _install_fake_websockets(monkeypatch, ws, capture)

        tts = ElevenLabsTTS(api_key="k", output_format=OUTPUT_PCM_16000)
        _ = [c async for c in tts.synthesize_stream("Hi.")]
        assert "output_format=pcm_16000" in capture["url"]
        assert tts.output_format == OUTPUT_PCM_16000

    async def test_unknown_format_coerced_to_ulaw(self):
        assert ElevenLabsTTS(api_key="k", output_format="mp3_44100").output_format == OUTPUT_ULAW_8000

    async def test_per_call_voice_and_model_override(self, monkeypatch):
        ws = FakeWS([json.dumps({"isFinal": True})])
        capture: dict = {}
        _install_fake_websockets(monkeypatch, ws, capture)

        tts = ElevenLabsTTS(api_key="k", voice_id="default-v")
        _ = [c async for c in tts.synthesize_stream("Hi.", voice="de-v", model="eleven_multilingual_v2")]
        assert "/de-v/stream-input" in capture["url"]
        assert "model_id=eleven_multilingual_v2" in capture["url"]


class TestVoiceSettings:
    async def test_init_message_carries_voice_settings(self, monkeypatch):
        ws = FakeWS([json.dumps({"isFinal": True})])
        _install_fake_websockets(monkeypatch, ws, {})

        tts = ElevenLabsTTS(api_key="k", stability=0.3, similarity=0.9, speed=1.1, style=0.2)
        _ = [c async for c in tts.synthesize_stream("Hi.")]

        init = json.loads(ws.sent[0])
        assert init["voice_settings"] == {
            "stability": 0.3,
            "similarity_boost": 0.9,
            "speed": 1.1,
            "style": 0.2,
        }

    async def test_defaults_omit_speed_and_style(self, monkeypatch):
        ws = FakeWS([json.dumps({"isFinal": True})])
        _install_fake_websockets(monkeypatch, ws, {})

        tts = ElevenLabsTTS(api_key="k")
        _ = [c async for c in tts.synthesize_stream("Hi.")]

        init = json.loads(ws.sent[0])
        assert init["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.75}


class TestCancellationAndFailure:
    async def test_cancel_mid_stream_is_clean(self, monkeypatch):
        ws = FakeWS([_audio_msg(b"c1"), _audio_msg(b"c2"), _audio_msg(b"c3")])
        _install_fake_websockets(monkeypatch, ws, {})

        tts = ElevenLabsTTS(api_key="k")
        received = []
        async for chunk in tts.synthesize_stream("One. Two. Three."):
            received.append(chunk)
            await tts.cancel()  # barge-in after first chunk

        assert received == [b"c1"]
        assert ws.closed

    async def test_stream_failure_raises_synthesis_error(self, monkeypatch):
        ws = FakeWS([], fail_on_recv=True)
        _install_fake_websockets(monkeypatch, ws, {})

        tts = ElevenLabsTTS(api_key="k")
        with pytest.raises(TTSSynthesisError):
            _ = [c async for c in tts.synthesize_stream("Hi.")]
        assert ws.closed

    async def test_first_chunk_latency_recorded(self, monkeypatch):
        ws = FakeWS([_audio_msg(b"c1"), json.dumps({"isFinal": True})])
        _install_fake_websockets(monkeypatch, ws, {})

        tts = ElevenLabsTTS(api_key="k")
        _ = [c async for c in tts.synthesize_stream("Hi.")]
        assert tts.last_first_chunk_ms is not None
        assert tts.last_first_chunk_ms >= 0.0
