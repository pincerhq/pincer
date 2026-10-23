"""Media Streams: Twilio's mu-law goes to Deepgram as-is.

Every 20 ms frame used to be decoded to PCM16 and upsampled to 16 kHz in pure
Python on the event loop (~90 µs per frame, 4x the bytes upstream) for a
conversion Deepgram does itself. linear16 survives one release behind
PINCER_VOICE_STT_ENCODING.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from unittest.mock import MagicMock

import pytest
from test_voice_streaming_turn import _settings
from voice_harness.transport_fakes import FakeSTT, FakeSTTStream

from pincer.voice.audio import mulaw8k_to_pcm16k
from pincer.voice.engine import CallDirection, MediaStreamEngine
from pincer.voice.stt import DeepgramSTT, STTConfig, stt_config_for_language

FRAME = bytes(range(160))  # one 20 ms Twilio frame, mu-law 8 kHz
TWILIO_FORMAT = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}


class RecordingSTTStream(FakeSTTStream):
    def __init__(self) -> None:
        super().__init__()
        self.audio: list[bytes] = []

    async def send_audio(self, audio_bytes: bytes) -> None:
        self.audio.append(audio_bytes)


class RecordingSTT(FakeSTT):
    def __init__(self) -> None:
        super().__init__()
        self.configs: list[STTConfig] = []

    async def start_stream(self, config=None):
        self.configs.append(config)
        self.streams.append(RecordingSTTStream())
        return self.streams[-1]


def _media_settings(encoding: str = ""):
    settings = _settings()
    settings.voice_stt_endpointing_ms = 0
    settings.voice_stt_utterance_end_ms = 0
    settings.voice_stt_encoding = encoding
    return settings


async def _media_call(encoding: str = "", media_format: dict | None = None):
    engine = MediaStreamEngine(_media_settings(encoding))
    engine._stt_provider = RecordingSTT()
    state = await engine.on_call_start("CA_ms", "+4930123456", CallDirection.INBOUND, language="de")
    if media_format is not None:
        state.metadata["media_format"] = media_format
    await engine.setup_media_stream_stt("CA_ms", "MZ_1")
    return engine, engine._stt_provider


def _b64(frame: bytes) -> str:
    return base64.b64encode(frame).decode()


class TestConfig:
    def test_default_is_twilio_native(self):
        config = STTConfig()
        assert (config.encoding, config.sample_rate, config.channels) == ("mulaw", 8000, 1)

    def test_per_language_config_keeps_mulaw_and_tuning(self):
        config = stt_config_for_language("de", _media_settings())
        assert (config.encoding, config.sample_rate) == ("mulaw", 8000)
        assert (config.endpointing, config.utterance_end_ms) == (400, 1000)

    def test_linear16_override(self):
        config = stt_config_for_language("en", _media_settings("LINEAR16"))
        assert (config.encoding, config.sample_rate) == ("linear16", 16000)
        assert config.endpointing == 300

    @pytest.mark.parametrize(
        ("encoding", "wire"),
        [
            ("", "&sample_rate=8000&channels=1&encoding=mulaw&"),
            ("linear16", "&sample_rate=16000&channels=1&encoding=linear16&"),
        ],
    )
    async def test_deepgram_url_carries_the_wire_format(self, monkeypatch, encoding, wire):
        captured = {}

        async def _connect(url, **kwargs):
            captured["url"] = url
            return MagicMock()

        monkeypatch.setattr("websockets.connect", _connect)
        monkeypatch.setattr("pincer.voice.stt.DeepgramSTTStream.start_listening", lambda self: asyncio.sleep(0))
        await DeepgramSTT("key").start_stream(stt_config_for_language("de", _media_settings(encoding)))

        assert wire in captured["url"]


class TestFramePath:
    async def test_twilio_frames_reach_deepgram_unchanged(self, monkeypatch):
        def _boom(_):
            raise AssertionError("mulaw8k_to_pcm16k must not run on the mu-law path")

        monkeypatch.setattr("pincer.voice.audio.mulaw8k_to_pcm16k", _boom)
        engine, stt = await _media_call()

        await engine.on_speech_input("CA_ms", _b64(FRAME))

        assert engine.get_call_state("CA_ms").metadata["stt_encoding"] == "mulaw"
        assert stt.streams[0].audio == [FRAME]
        await engine.close_media_stream("CA_ms")

    async def test_linear16_sends_the_upsampled_pcm(self):
        engine, stt = await _media_call("linear16")

        await engine.on_speech_input("CA_ms", _b64(FRAME))

        assert (stt.configs[0].encoding, stt.configs[0].sample_rate) == ("linear16", 16000)
        assert stt.streams[0].audio == [mulaw8k_to_pcm16k(FRAME)]
        assert len(stt.streams[0].audio[0]) == 640
        await engine.close_media_stream("CA_ms")

    @pytest.mark.parametrize("encoding", ["", "linear16"])
    async def test_reconnect_keeps_the_encoding(self, encoding):
        engine, stt = await _media_call(encoding)
        expected = stt.configs[0].encoding

        await engine._recover_stt_stream("CA_ms", "MZ_1")
        await engine.on_speech_input("CA_ms", _b64(FRAME))

        assert [c.encoding for c in stt.configs] == [expected, expected]
        assert engine.get_call_state("CA_ms").metadata["stt_encoding"] == expected
        assert len(stt.streams[-1].audio[0]) == (160 if expected == "mulaw" else 640)
        await engine.close_media_stream("CA_ms")


class TestMediaFormat:
    async def test_twilio_format_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="pincer.voice.engine"):
            engine, _ = await _media_call(media_format=TWILIO_FORMAT)
        assert "not mu-law 8 kHz mono" not in caplog.text
        await engine.close_media_stream("CA_ms")

    async def test_other_format_is_reported(self, caplog):
        with caplog.at_level(logging.WARNING, logger="pincer.voice.engine"):
            engine, _ = await _media_call(media_format={**TWILIO_FORMAT, "sampleRate": 16000})
        assert "not mu-law 8 kHz mono" in caplog.text
        await engine.close_media_stream("CA_ms")


@pytest.mark.perf
@pytest.mark.skipif(not os.environ.get("PINCER_RUN_PERF"), reason="timing benchmark; run with PINCER_RUN_PERF=1")
async def test_thousand_frames_cost_under_20ms():
    engine, _ = await _media_call()
    state = engine.get_call_state("CA_ms")

    class _NoOp:
        async def send_audio(self, audio_bytes: bytes) -> None:
            pass

    state.metadata["stt_stream"] = _NoOp()
    payload = _b64(FRAME)
    start = time.perf_counter()
    for _ in range(1000):
        await engine.on_speech_input("CA_ms", payload)
    assert time.perf_counter() - start < 0.020
