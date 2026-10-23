"""Media Streams barge-in through the real transcript consumer.

The channel-level tests prove a running turn can be cancelled; these prove the
production path reaches that code. Each drives a real MediaStreamEngine — its
`_consume_transcripts` loop and `_stream_tts` — with fake Deepgram/ElevenLabs/
Twilio ends (voice_harness.transport_fakes) and a scripted LLM that stalls
mid-reply. Synchronisation is by events: `stt.queue.join()` returns once the
consumer has acted on everything fed to it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_voice_streaming_turn import FakeStreamAgent, _settings, text_chunks
from voice_harness.transport_fakes import FakeSocket, FakeSTT, FakeSTTStream, FakeTTS, FakeTwilio, install_fake_twilio

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import scheduling
from pincer.voice.engine import BARGE_IN_ARM_DELAY_MS, CallDirection, MediaStreamEngine
from pincer.voice.stt import DeepgramSTTStream, STTConfig, Transcript, TranscriptWord

OLD_FIRST = "Der erste Satz der alten Antwort."
OLD_NEVER = "Dieser Satz darf niemals gesprochen werden."
NEW_REPLY = "Die neue Antwort auf die Unterbrechung."

SPEECH_STARTED = Transcript(text="", speech_started=True, timestamp_s=1.2)


@pytest.fixture(autouse=True)
def _clean_scheduling():
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_twilio_rest(monkeypatch):
    """A real engine: ending a call would hit the Twilio REST API."""
    install_fake_twilio(monkeypatch, FakeTwilio)


def _engine(tts: FakeTTS, trigger: str = "vad") -> MediaStreamEngine:
    settings = _settings()
    settings.voice_stt_endpointing_ms = 0
    settings.voice_stt_utterance_end_ms = 0
    settings.voice_barge_in_trigger = trigger
    settings.voice_barge_in_min_words = 1
    engine = MediaStreamEngine(settings)
    engine._stt_provider = FakeSTT()
    engine._tts_provider = tts
    return engine


async def _channel_for(engine: MediaStreamEngine, scripts) -> tuple[VoiceChannel, FakeStreamAgent]:
    channel = VoiceChannel(engine._settings)
    channel.set_engine(engine)
    agent = FakeStreamAgent(scripts)
    channel.set_stream_agent(agent)

    async def _handler(_incoming):
        return ""

    await channel.start(_handler)
    return channel, agent


async def _call(engine: MediaStreamEngine, call_sid: str) -> tuple[FakeSocket, FakeSTTStream]:
    await engine.on_call_start(call_sid, "+4930123456", CallDirection.INBOUND, language="de")
    sock = FakeSocket()
    engine.get_call_state(call_sid).metadata["websocket"] = sock
    await engine.setup_media_stream_stt(call_sid, f"MZ_{call_sid}")
    return sock, engine._stt_provider.streams[-1]


def _final(text: str) -> Transcript:
    return Transcript(text=text, is_final=True, confidence=0.99)


async def _fed(stt: FakeSTTStream, *transcripts: Transcript) -> None:
    """Feed transcripts and return once the consumer has acted on all of them."""
    for transcript in transcripts:
        stt.queue.put_nowait(transcript)
    await asyncio.wait_for(stt.queue.join(), timeout=2)


async def _agent_mid_reply(trigger: str = "vad", *, armed: bool = True):
    """A call whose reply has played its first sentence (2 s of audio still
    queued at Twilio) and whose LLM is stalled before the second."""
    engine = _engine(FakeTTS(), trigger)
    channel, agent = await _channel_for(engine, [text_chunks(f"{OLD_FIRST} ", OLD_NEVER), text_chunks(NEW_REPLY)])
    sock, stt = await _call(engine, "CA_ms")
    agent.pause_after = 1
    await _fed(stt, _final("Erste Frage?"))
    await asyncio.wait_for(agent.paused.wait(), timeout=2)
    state = engine.get_call_state("CA_ms")
    if armed:  # our audio started longer ago than the arm delay
        state.metadata["playout_started_ns"] -= (BARGE_IN_ARM_DELAY_MS + 50) * 1_000_000
    agent.pause_after = None
    return engine, channel, agent, sock, stt, state


class TestConsumer:
    # regression: fails on dev @ 0a1c320 (B is not consumed until A finishes)
    async def test_a_new_utterance_is_read_while_a_turn_is_running(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("off")
        old_turn = channel._active_turns["CA_ms"]

        await _fed(stt, _final("Moment, etwas anderes!"))
        await channel.wait_for_turn("CA_ms")

        assert old_turn.cancelled()  # while the LLM was still stalled
        assert engine._tts_provider.synthesized == [OLD_FIRST, NEW_REPLY]
        await channel.stop()

    # regression: fails on dev @ 0a1c320
    async def test_low_confidence_reask_does_not_block_the_consumer(self):
        tts = FakeTTS()
        tts.hold_first = asyncio.Event()  # the re-ask's synthesis never gets going
        engine = _engine(tts)
        channel, agent = await _channel_for(engine, [text_chunks(NEW_REPLY)])
        sock, stt = await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        await _fed(stt, Transcript(text="Mmh wie bitte", is_final=True, confidence=0.1))
        await asyncio.wait_for(tts.started.wait(), timeout=2)
        reask = state.metadata["aux_speech_task"]

        # Read and acted on although the re-ask is still "speaking"
        await _fed(stt, _final("Ich möchte einen Termin."))
        await channel.wait_for_turn("CA_ms")

        assert reask.cancelled()  # it never plays over the reply
        assert agent.calls[0]["text"] == "Ich möchte einen Termin."
        assert tts.synthesized[-1] == NEW_REPLY
        await channel.stop()


class TestDetection:
    # regression: fails on dev @ 0a1c320 (nothing reacts to the caller)
    async def test_speech_started_during_playout_stops_the_agent(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        interrupts: list[str] = []
        channel_barge_in = engine._on_barge_in_callback

        async def _spy(call_sid, heard=None):
            interrupts.append(call_sid)
            await channel_barge_in(call_sid, heard)

        engine.set_on_barge_in(_spy)
        epoch = state.metadata.get("speech_epoch", 0)

        await _fed(stt, SPEECH_STARTED)

        assert len(sock.events("clear")) == 1
        assert interrupts == ["CA_ms"]
        assert state.metadata["speech_epoch"] == epoch + 1  # this call's synthesis stopped
        assert "CA_ms" not in channel._active_turns
        reactions = engine.metrics_registry.get("CA_ms").barge_in_reactions_ms
        assert len(reactions) == 1 and reactions[0] < 500

        await _fed(stt, _final("Moment, etwas anderes!"))
        await channel.wait_for_turn("CA_ms")
        assert OLD_NEVER not in engine._tts_provider.synthesized
        assert engine._tts_provider.synthesized[-1] == NEW_REPLY
        await channel.stop()

    # regression: fails on dev @ 0a1c320
    async def test_interim_words_stop_the_agent(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()

        await _fed(stt, Transcript(text="Moment", words=[TranscriptWord("Moment", start=0.1, end=0.4)]))

        assert len(sock.events("clear")) == 1
        assert "CA_ms" not in channel._active_turns
        assert len(engine.metrics_registry.get("CA_ms").barge_in_reactions_ms) == 1
        await channel.stop()

    async def test_interim_mode_ignores_vad_and_waits_for_words(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("interim")

        await _fed(stt, SPEECH_STARTED)
        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns

        await _fed(stt, Transcript(text="Moment"))
        assert len(sock.events("clear")) == 1
        assert "CA_ms" not in channel._active_turns
        await channel.stop()

    async def test_off_mode_never_interrupts(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("off")

        await _fed(stt, SPEECH_STARTED, Transcript(text="Moment"))

        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns
        await channel.stop()

    async def test_speech_started_while_silent_is_not_an_interruption(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        state.metadata["playout_until_ns"] = 0  # Twilio has played everything out

        await _fed(stt, SPEECH_STARTED)

        assert sock.events("clear") == []
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []
        await channel.stop()

    async def test_speech_started_inside_the_arm_delay_is_ignored(self):
        """An onset right after our audio began is most likely our own echo."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply(armed=False)

        await _fed(stt, SPEECH_STARTED)

        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns
        await channel.stop()


class TestPerCallStop:
    # regression: fails on dev @ 0a1c320 (the provider-wide cancel cut call B)
    async def test_interrupt_stops_only_that_calls_audio(self):
        """The TTS provider is shared by every call; a barge-in on one call
        must not cut the audio of another."""
        tts = FakeTTS(chunks=4, delay_s=0.01)
        engine = _engine(tts)
        sock_a, _ = await _call(engine, "CA_a")
        sock_b, _ = await _call(engine, "CA_b")

        speaking_b = asyncio.create_task(engine.send_speech("CA_b", "Ein langer Satz für Anruf B."))
        await sock_b.wait_sent(lambda sent: any(m.get("event") == "media" for m in sent))
        await engine.interrupt_speech("CA_a")
        assert await asyncio.wait_for(speaking_b, timeout=2) is True

        assert len(sock_b.events("media")) == 4
        assert sock_b.events("clear") == []

    async def test_interrupt_stops_the_rest_of_the_utterance(self):
        """Later segments of an interrupted utterance are never synthesized."""
        tts = FakeTTS(chunks=2, delay_s=0.01)
        engine = _engine(tts)
        sock, _ = await _call(engine, "CA_ms")

        speaking = asyncio.create_task(engine.send_speech("CA_ms", "Erster Satz hier. Zweiter Satz dort. Dritter."))
        await sock.wait_sent(lambda sent: any(m.get("event") == "media" for m in sent))
        await engine.interrupt_speech("CA_ms")
        await asyncio.wait_for(speaking, timeout=2)

        assert tts.synthesized == ["Erster Satz hier."]
        assert len(sock.events("media")) == 1


class _DeepgramFrames:
    """The websocket a DeepgramSTTStream listens on, replaying fixed frames."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = [json.dumps(f) for f in frames]

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def close(self) -> None: ...


async def test_deepgram_speech_started_becomes_a_transcript_event():
    words = [{"word": "hallo", "start": 0.5, "end": 0.9, "confidence": 0.9}]
    frames = [
        {"type": "SpeechStarted", "timestamp": 1.25},
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "hallo", "words": words}]}},
        {"type": "UtteranceEnd"},
    ]
    stream = DeepgramSTTStream(_DeepgramFrames(frames), STTConfig())
    await stream.start_listening()
    await stream._listen_task

    got = [stream._transcript_queue.get_nowait() for _ in range(3)]
    assert got[0].speech_started and got[0].text == "" and got[0].timestamp_s == 1.25
    assert got[1].text == "hallo" and got[1].is_final and not got[1].speech_started
    assert got[2].text == "" and got[2].is_final and got[2].speech_final and not got[2].speech_started
