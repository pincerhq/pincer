"""ElevenLabs outage resilience (Sprint 4, T4.5): retry once, spoken fallback,
clean teardown — never dead air, on either engine."""

from __future__ import annotations

import base64
import json
import sys
import types
from unittest.mock import MagicMock

from pincer.voice.engine import CallDirection, MediaStreamEngine
from pincer.voice.metrics import VoiceMetricsRegistry
from pincer.voice.tts import OUTPUT_PCM_16000, OUTPUT_ULAW_8000, TTSSynthesisError


class FlakyTTS:
    """TTS provider that fails the first `fail_times` synthesis attempts."""

    output_format = OUTPUT_ULAW_8000

    def __init__(self, fail_times: int = 0, chunks: list[bytes] | None = None):
        self.fail_times = fail_times
        self.chunks = chunks if chunks is not None else [b"ulaw-audio"]
        self.attempts = 0
        self.cancelled = False

    async def synthesize_stream(self, text, voice=None, model=None):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise TTSSynthesisError("elevenlabs down")
        for chunk in self.chunks:
            yield chunk

    async def cancel(self):
        self.cancelled = True


class MidStreamFailTTS:
    """Fails *after* streaming part of a segment — the case a whole-utterance
    retry turns into duplicated speech.

    Each synthesized segment yields two identifiable chunks; the failure lands
    between them on the `fail_on_nth_call`-th synthesis (0-based) and happens
    only once, so the retry succeeds.
    """

    output_format = OUTPUT_ULAW_8000

    def __init__(self, fail_on_nth_call: int | None = None):
        self.fail_on_nth_call = fail_on_nth_call
        self.requested: list[str] = []
        self.cancelled = False

    async def synthesize_stream(self, text, voice=None, model=None):
        index = len(self.requested)
        self.requested.append(text)
        head = text.split()[0][:6].encode()
        yield b"A:" + head
        if index == self.fail_on_nth_call:
            self.fail_on_nth_call = None  # fail once, then recover
            raise TTSSynthesisError("stream died mid-utterance")
        yield b"B:" + head

    async def cancel(self):
        self.cancelled = True


class FakeStreamWS:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, msg: str) -> None:
        self.messages.append(json.loads(msg))

    def audio(self) -> list[bytes]:
        return [base64.b64decode(m["media"]["payload"]) for m in self.messages if m.get("event") == "media"]

    def events(self) -> list[str]:
        return [m["event"] for m in self.messages if m.get("event") != "media"]


def _settings():
    settings = MagicMock()
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de"
    settings.voice_language = "en-US"
    settings.voice_de_formality = "sie"
    settings.twilio_account_sid = "AC1"
    settings.twilio_auth_token.get_secret_value.return_value = "tok"
    settings.deepgram_api_key.get_secret_value.return_value = ""
    settings.elevenlabs_api_key.get_secret_value.return_value = ""
    return settings


def _install_fake_twilio(monkeypatch, captured: dict):
    class FakeCall:
        def __init__(self, sid):
            self._sid = sid

        def update(self, **kwargs):
            captured.setdefault(self._sid, []).append(kwargs)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = MagicMock(side_effect=FakeCall)

    twilio_rest = types.ModuleType("twilio.rest")
    twilio_rest.Client = FakeClient
    twilio_mod = types.ModuleType("twilio")
    twilio_mod.rest = twilio_rest
    monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
    monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)


async def _start_call(engine, language="en"):
    state = await engine.on_call_start("CA_tts", "+15550001111", CallDirection.OUTBOUND, language=language)
    ws = FakeStreamWS()
    state.metadata["websocket"] = ws
    state.metadata["stream_sid"] = "MZ1"
    return state, ws


class TestRetryOnce:
    async def test_single_failure_recovers_silently(self, monkeypatch):
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=1)
        state, ws = await _start_call(engine)

        await engine.send_speech("CA_tts", "Hello caller.")

        assert engine._tts_provider.attempts == 2  # failed once, retried, succeeded
        media = [m for m in ws.messages if m.get("event") == "media"]
        assert media and base64.b64decode(media[0]["media"]["payload"]) == b"ulaw-audio"
        assert engine.get_call_state("CA_tts") is not None  # call keeps going

    async def test_ulaw_chunks_pass_through_untouched(self, monkeypatch):
        """No PCM downsampling in the hot path (T4.2)."""
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(chunks=[b"\x00\x01\x02"])
        state, ws = await _start_call(engine)

        def _boom(_data):
            raise AssertionError("pcm16k_to_mulaw8k must not run on the ulaw path")

        monkeypatch.setattr("pincer.voice.audio.pcm16k_to_mulaw8k", _boom)
        await engine.send_speech("CA_tts", "Hi.")
        media = [m for m in ws.messages if m.get("event") == "media"]
        assert base64.b64decode(media[0]["media"]["payload"]) == b"\x00\x01\x02"

    async def test_pcm_fallback_path_still_resamples(self, monkeypatch):
        engine = MediaStreamEngine(_settings())
        tts = FlakyTTS(chunks=[b"\x00\x00" * 4])
        tts.output_format = OUTPUT_PCM_16000
        engine._tts_provider = tts
        state, ws = await _start_call(engine)

        called = {}

        def _fake_resample(data):
            called["data"] = data
            return b"resampled"

        monkeypatch.setattr("pincer.voice.audio.pcm16k_to_mulaw8k", _fake_resample)
        await engine.send_speech("CA_tts", "Hi.")
        media = [m for m in ws.messages if m.get("event") == "media"]
        assert base64.b64decode(media[0]["media"]["payload"]) == b"resampled"
        assert called["data"] == b"\x00\x00" * 4


THREE_SENTENCES = "I will book you for Tuesday at three. The clinic is on Hauptstrasse. See you then."


class TestRetryResumesInsteadOfRepeating:
    """A mid-stream failure used to re-speak the utterance from the start, so
    the caller heard the opening sentences twice while the transcript recorded
    them once. Delivery is tracked per sentence-segment instead."""

    async def test_segments_split_as_expected(self):
        """Anchors the fixture the rest of the class reasons about."""
        from pincer.voice.sentence_stream import split_into_segments

        assert len(split_into_segments(THREE_SENTENCES)) == 3

    async def test_completed_segments_are_not_respoken(self):
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=1)  # dies inside segment 1
        _state, ws = await _start_call(engine)

        assert await engine.send_speech("CA_tts", THREE_SENTENCES) is True

        # Segment 0 was already delivered: it must not be synthesized again.
        assert engine._tts_provider.requested.count("I will book you for Tuesday at three.") == 1
        assert engine._tts_provider.requested.count("See you then.") == 1
        # Only the segment that failed is retried.
        assert engine._tts_provider.requested.count("The clinic is on Hauptstrasse.") == 2

    async def test_delivered_audio_never_repeats_a_finished_segment(self):
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=1)
        _state, ws = await _start_call(engine)

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        audio = ws.audio()
        # Segment 0's chunks reach Twilio exactly once each.
        assert audio.count(b"A:I") == 1
        assert audio.count(b"B:I") == 1
        # Segment 2 likewise — the tail is not dragged back through the retry.
        assert audio.count(b"A:See") == 1
        assert audio.count(b"B:See") == 1

    async def test_partial_first_segment_is_cleared_before_retry(self):
        """Nothing complete is buffered yet, so dropping the fragment is safe
        and spares the caller hearing it twice."""
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=0)
        _state, ws = await _start_call(engine)

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        assert "clear" in ws.events()

    async def test_no_clear_once_a_segment_has_completed(self):
        """`clear` flushes Twilio's whole outbound buffer, so sending it after
        a segment completed would silently swallow speech the caller has not
        heard yet. Losing content is worse than one stutter."""
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=1)
        _state, ws = await _start_call(engine)

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        assert "clear" not in ws.events()

    async def test_no_clear_when_the_failure_sent_no_audio(self):
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=1)  # raises before any chunk
        _state, ws = await _start_call(engine)

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        assert "clear" not in ws.events()

    async def test_retry_does_not_double_bill_characters(self):
        """The old whole-utterance retry charged every character twice."""
        from pincer.voice.sentence_stream import split_into_segments

        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=1)
        state, _ws = await _start_call(engine)

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        expected = sum(len(s) for s in split_into_segments(THREE_SENTENCES))
        assert state.metadata["tts_characters"] == expected

    async def test_single_segment_utterance_still_recovers(self):
        """One sentence has no earlier segment to preserve: clear and redo."""
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=0)
        _state, ws = await _start_call(engine)

        assert await engine.send_speech("CA_tts", "Hello there caller.") is True
        assert engine._tts_provider.requested == ["Hello there caller.", "Hello there caller."]
        assert "clear" in ws.events()

    async def test_second_failure_still_takes_the_spoken_fallback(self, monkeypatch):
        """Resuming must not swallow the outage path."""
        captured: dict = {}
        _install_fake_twilio(monkeypatch, captured)
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=2)
        await _start_call(engine)

        assert await engine.send_speech("CA_tts", THREE_SENTENCES) is False
        assert engine.get_call_state("CA_tts") is None, "the call is torn down after a second failure"

    async def test_first_chunk_latency_recorded_once_per_utterance(self):
        engine = MediaStreamEngine(_settings())
        registry = VoiceMetricsRegistry()
        engine.metrics_registry = registry
        engine._tts_provider = MidStreamFailTTS(fail_on_nth_call=1)
        await _start_call(engine)
        registry.start_call("CA_tts", engine="media_streams")

        await engine.send_speech("CA_tts", THREE_SENTENCES)

        from pincer.voice.sentence_stream import split_into_segments

        metrics = registry.get("CA_tts")
        assert len(metrics.tts_first_chunk_ms) == 1, "playout begins once, whichever attempt sent the first chunk"
        # And the character count is the utterance, not the utterance + retry.
        assert metrics.tts_characters == sum(len(s) for s in split_into_segments(THREE_SENTENCES))


class TestSpokenFallback:
    async def test_double_failure_speaks_apology_and_ends(self, monkeypatch):
        """Acceptance: outage mid-call → graceful spoken fallback, clean end."""
        captured: dict = {}
        _install_fake_twilio(monkeypatch, captured)

        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=99)
        state, ws = await _start_call(engine)

        ended = []

        async def _on_end(call_sid, call_state):
            ended.append(call_sid)

        engine.set_on_call_end(_on_end)

        await engine.send_speech("CA_tts", "Hello caller.")

        assert engine._tts_provider.attempts == 2  # exactly one retry
        updates = captured.get("CA_tts", [])
        assert updates, "no fallback TwiML sent — dead air"
        twiml = updates[0]["twiml"]
        assert "<Say" in twiml and "<Hangup/>" in twiml
        assert "technical problem" in twiml  # spoken apology, not silence
        assert engine.get_call_state("CA_tts") is None  # unregistered
        assert ended == ["CA_tts"]

    async def test_german_call_gets_german_apology(self, monkeypatch):
        captured: dict = {}
        _install_fake_twilio(monkeypatch, captured)

        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=99)
        state, ws = await _start_call(engine, language="de")
        await engine.send_speech("CA_tts", "Hallo.")

        twiml = captured["CA_tts"][0]["twiml"]
        assert 'language="de-DE"' in twiml
        assert "Sprachausgabe" in twiml

    async def test_twilio_also_down_ends_call_directly(self, monkeypatch):
        class BrokenClient:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("twilio down")

        twilio_rest = types.ModuleType("twilio.rest")
        twilio_rest.Client = BrokenClient
        twilio_mod = types.ModuleType("twilio")
        twilio_mod.rest = twilio_rest
        monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
        monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)

        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS(fail_times=99)
        state, ws = await _start_call(engine)
        await engine.send_speech("CA_tts", "Hello.")
        # end_call also needs Twilio, but the call must be unregistered locally
        assert engine.get_call_state("CA_tts") is None


class TestTTSMetrics:
    async def test_characters_and_first_chunk_recorded(self):
        engine = MediaStreamEngine(_settings())
        engine._tts_provider = FlakyTTS()
        registry = VoiceMetricsRegistry()
        engine.metrics_registry = registry
        state, ws = await _start_call(engine)
        registry.start_call("CA_tts", engine="media_streams")

        await engine.send_speech("CA_tts", "Hello caller.")

        metrics = registry.get("CA_tts")
        assert metrics.tts_characters == len("Hello caller.")
        assert len(metrics.tts_first_chunk_ms) == 1
        assert state.metadata["tts_characters"] == len("Hello caller.")
        summary = metrics.summary()
        assert summary["tts_characters"] == len("Hello caller.")
