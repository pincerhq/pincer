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
from pincer.voice.engine import BARGE_IN_ARM_DELAY_MS, BARGE_IN_CONFIRM_WINDOW_MS, CallDirection, MediaStreamEngine
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


_REAL_WAIT_FOR_PLAYOUT = MediaStreamEngine.wait_for_playout


async def _no_wait_for_playout(self, call_sid: str) -> None:
    """FakeTTS writes seconds of audio nobody plays; `channel.stop()` must
    not sleep them out. The shutdown test restores the real wait."""


@pytest.fixture(autouse=True)
def _instant_playout(monkeypatch):
    monkeypatch.setattr(MediaStreamEngine, "wait_for_playout", _no_wait_for_playout)


def _engine(tts: FakeTTS, trigger: str = "vad", min_words: int = 1) -> MediaStreamEngine:
    settings = _settings()
    settings.voice_stt_endpointing_ms = 0
    settings.voice_stt_utterance_end_ms = 0
    settings.voice_barge_in_trigger = trigger
    settings.voice_barge_in_min_words = min_words
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


async def _agent_mid_reply(trigger: str = "vad", *, armed: bool = True, min_words: int = 1):
    """A call whose reply has played its first sentence (2 s of audio still
    queued at Twilio) and whose LLM is stalled before the second."""
    engine = _engine(FakeTTS(), trigger, min_words)
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

    async def _reask_sent(self, *, played_out: bool):
        """The re-ask has been fully sent (2 s of audio queued at Twilio).
        `off` mode, so no barge-in can be what clears it."""
        engine = _engine(FakeTTS(), "off")
        channel, agent = await _channel_for(engine, [text_chunks(NEW_REPLY)])
        sock, stt = await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        await _fed(stt, Transcript(text="Mmh wie bitte", is_final=True, confidence=0.1))
        await state.metadata["aux_speech_task"]
        assert sock.events("media")
        if played_out:
            state.metadata["playout_until_ns"] = 0
        sent_before = len(sock.sent)

        await _fed(stt, _final("Ich möchte einen Termin."))
        await channel.wait_for_turn("CA_ms")
        return engine, channel, sock, [m.get("event") for m in sock.sent[sent_before:]]

    async def test_a_sent_reask_is_cleared_before_the_reply(self):
        engine, channel, sock, after = await self._reask_sent(played_out=False)

        assert after[0] == "clear"  # before any of the reply's audio
        assert "media" in after[1:]
        assert engine._tts_provider.synthesized[-1] == NEW_REPLY
        await channel.stop()

    async def test_a_played_out_reask_is_not_cleared(self):
        engine, channel, sock, after = await self._reask_sent(played_out=True)

        assert "clear" not in after
        await channel.stop()

    async def test_clearing_a_reask_and_cancelling_the_turn_count_one_interruption(self, monkeypatch):
        """`off` mode, a turn still playing, a re-ask on top of it: the next
        final clears the re-ask and cancels the turn, one interruption."""
        from pincer.voice.analytics import get_accumulator
        from pincer.voice.telemetry import hooks

        detected: list[str] = []
        monkeypatch.setattr(
            hooks, "interruption_detected", lambda call_sid, **attrs: detected.append(attrs["source"]) or 0
        )
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("off")
        before = get_accumulator(state).interruptions
        await _fed(stt, Transcript(text="Mmh wie bitte", is_final=True, confidence=0.1))
        await state.metadata["aux_speech_task"]  # the re-ask's audio is at Twilio too

        await _fed(stt, _final("Ich möchte einen Termin."))
        await channel.wait_for_turn("CA_ms")

        assert get_accumulator(state).interruptions == before + 1
        assert detected == ["next_utterance"]
        await channel.stop()


class TestDetection:
    # regression: fails on dev @ 0a1c320 (nothing reacts to the caller)
    async def test_speech_started_then_words_stops_the_agent(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        interrupts: list[str] = []
        channel_barge_in = engine._on_barge_in_callback

        async def _spy(call_sid, heard=None):
            interrupts.append(call_sid)
            await channel_barge_in(call_sid, heard)

        engine.set_on_barge_in(_spy)
        epoch = state.metadata.get("speech_epoch", 0)

        await _fed(stt, SPEECH_STARTED)
        assert sock.events("clear") == []  # a candidate until words confirm it
        await _fed(stt, Transcript(text="Moment"))

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

    async def test_speech_started_without_words_keeps_the_reply(self):
        """A cough: VAD fires, no transcript follows, the reply plays on."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()

        await _fed(stt, SPEECH_STARTED)
        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns

        agent.resume.set()
        await channel.wait_for_turn("CA_ms")
        assert engine._tts_provider.synthesized == [OLD_FIRST, OLD_NEVER]
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []
        await channel.stop()

    async def test_vad_onset_confirms_on_fewer_words_than_interim_needs(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        engine._settings.voice_barge_in_min_words = 3
        # min_words is read when the consumer starts
        await engine.close_media_stream("CA_ms")
        await engine.setup_media_stream_stt("CA_ms", "MZ_CA_ms")
        stt = engine._stt_provider.streams[-1]

        await _fed(stt, Transcript(text="Moment"))
        assert sock.events("clear") == []  # one word, no onset: below min_words

        await _fed(stt, SPEECH_STARTED, Transcript(text="Moment"))
        assert len(sock.events("clear")) == 1
        await channel.stop()

    async def test_a_stale_vad_onset_does_not_confirm(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        engine._settings.voice_barge_in_min_words = 3
        await engine.close_media_stream("CA_ms")
        await engine.setup_media_stream_stt("CA_ms", "MZ_CA_ms")
        stt = engine._stt_provider.streams[-1]

        await _fed(stt, SPEECH_STARTED)
        state.metadata["vad_onset_ns"] -= (BARGE_IN_CONFIRM_WINDOW_MS + 100) * 1_000_000
        await _fed(stt, Transcript(text="Moment"))

        assert sock.events("clear") == []
        await channel.stop()

    async def test_a_vad_onset_from_the_last_reply_does_not_cut_the_next_one(self):
        """A short "ja" over our last syllable: VAD fires while we are audible,
        the words arrive after playout ended. The onset must not confirm the
        echo of the next reply's first syllable as a barge-in."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()

        await _fed(stt, SPEECH_STARTED)
        state.metadata["playout_until_ns"] = 0  # Twilio played the reply out
        await _fed(stt, _final("Ja"))  # not audible: the onset is not consumed here
        await channel.wait_for_turn("CA_ms")  # the next reply's audio is at Twilio
        clears = len(sock.events("clear"))

        await _fed(stt, Transcript(text="Die"))  # echo of its first syllable

        assert len(sock.events("clear")) == clears
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []
        await channel.stop()

    async def test_an_onset_whose_utterance_ended_does_not_confirm_a_later_word(self):
        """A cough: SpeechStarted, then UtteranceEnd (an empty final) with no
        words, all while we are still talking. One stray word in the same
        stretch of audio is below min_words; the finished cough's onset must
        not confirm it. Only the pop on the final stands between them: the
        onset is newer than this playout's start."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply(min_words=2)

        await _fed(stt, SPEECH_STARTED)
        assert state.metadata.get("vad_onset_ns"), "precondition: the onset was taken as a candidate"
        await _fed(stt, Transcript(text="", is_final=True))  # UtteranceEnd: the cough is over
        await _fed(stt, Transcript(text="Ja"))

        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns
        await channel.stop()

    async def test_an_onset_from_before_this_stretch_of_audio_does_not_confirm(self):
        """The onset came during the first sentence and no final ever came
        (noise, never transcribed). The reply drained while the LLM stalled,
        then its next sentence began a new stretch of audio. The echo of that
        sentence's first syllable must not confirm the old onset. The onset is
        still this reply's (no final popped it), so what stands between them is
        the echo check on VAD confirmation: an untimed interim inside the new
        stretch's arm window is our own audio, not the caller."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()

        await _fed(stt, SPEECH_STARTED)
        onset = state.metadata.get("vad_onset_ns")
        assert onset, "precondition: the onset was taken as a candidate"
        state.metadata["playout_until_ns"] = 0  # the first sentence played out
        agent.resume.set()
        await channel.wait_for_turn("CA_ms")  # the second sentence is at Twilio
        assert state.metadata["playout_started_ns"] > onset, "precondition: a new stretch of audio"
        assert state.metadata.get("vad_onset_ns") == onset, "precondition: nothing consumed the onset"

        await _fed(stt, Transcript(text="Die"))  # echo of its first syllable

        assert sock.events("clear") == []
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []
        await channel.stop()

    async def test_an_onset_from_earlier_in_the_same_reply_still_confirms(self):
        """The caller says "Stop" during sentence 1; the armed SpeechStarted is
        the onset. Sentence 2 starts a new stretch of audio after a short TTS
        gap, and only then does the word arrive. It is the same reply, so the
        onset still confirms it — with min_words 2, one word would not."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply(min_words=2)

        await _fed(stt, SPEECH_STARTED)
        onset = state.metadata.get("vad_onset_ns")
        assert onset, "precondition: the onset was taken as a candidate"
        state.metadata["playout_until_ns"] = 0  # sentence 1 played out before sentence 2 was ready
        agent.resume.set()
        await channel.wait_for_turn("CA_ms")
        assert state.metadata["playout_started_ns"] > onset, "precondition: a new stretch of audio"

        spoken_at = (onset - int(state.metadata["stt_started_ns"])) / 1_000_000_000
        await _fed(stt, Transcript(text="Stop", words=[TranscriptWord("Stop", start=spoken_at, end=spoken_at + 0.3)]))

        assert len(sock.events("clear")) == 1
        assert len(engine.metrics_registry.get("CA_ms").barge_in_reactions_ms) == 1
        await channel.stop()

    async def test_an_onset_from_before_a_new_reply_does_not_confirm_in_it(self):
        """The old reply is fully generated and still playing. The caller says
        "Mhm" (one word: no barge-in), and while the new reply is being
        generated a SpeechStarted lands on the old audio's tail. Once the new
        reply's first chunk is written, that onset is the old reply's: one
        stray word in the new reply must not confirm it."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply(min_words=2)
        agent.resume.set()
        await channel.wait_for_turn("CA_ms")  # the old reply is all at Twilio, still playing
        agent.resume, agent.paused = asyncio.Event(), asyncio.Event()
        agent.pause_after = 0  # the new reply stalls before its first chunk

        await _fed(stt, _final("Mhm"))
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        await _fed(stt, SPEECH_STARTED)
        assert state.metadata.get("vad_onset_ns"), "precondition: the onset was taken as a candidate"
        clears = len(sock.events("clear"))
        agent.pause_after = None
        agent.resume.set()
        await channel.wait_for_turn("CA_ms")  # the new reply's audio is at Twilio

        await _fed(stt, Transcript(text="Die"))

        assert len(sock.events("clear")) == clears
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []
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

    @staticmethod
    def _words_at(state, after_our_onset_ms: float) -> Transcript:
        """An interim whose speech began this long after our audio started."""
        offset_s = (state.metadata["playout_started_ns"] - state.metadata["stt_started_ns"]) / 1e9
        start = offset_s + after_our_onset_ms / 1000
        return Transcript(text="Moment", words=[TranscriptWord("Moment", start=start, end=start + 0.3)])

    async def test_interim_echo_of_our_onset_is_ignored(self):
        """Arrives late (Deepgram latency) but began inside the arm window."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("interim")

        await _fed(stt, self._words_at(state, BARGE_IN_ARM_DELAY_MS / 2))

        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns
        await channel.stop()

    async def test_interim_words_after_the_arm_window_stop_the_agent(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("interim")

        await _fed(stt, self._words_at(state, BARGE_IN_ARM_DELAY_MS + 100))

        assert len(sock.events("clear")) == 1
        assert "CA_ms" not in channel._active_turns
        await channel.stop()

    async def test_a_caller_who_keeps_talking_past_the_arm_window_stops_the_agent(self):
        """Caller and agent start at the same moment: every interim of the
        utterance begins inside the window, but its later words do not."""
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("interim")
        offset_s = (state.metadata["playout_started_ns"] - state.metadata["stt_started_ns"]) / 1e9
        first = offset_s + 0.1  # 100 ms after our audio began
        words = [
            TranscriptWord("Moment", start=first, end=first + 0.2),
            TranscriptWord("bitte", start=first + 0.4, end=first + 0.7),  # past the window
        ]

        await _fed(stt, Transcript(text="Moment bitte", words=words))

        assert len(sock.events("clear")) == 1
        assert "CA_ms" not in channel._active_turns
        await channel.stop()

    async def test_untimed_interim_inside_the_arm_window_is_ignored(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply("interim", armed=False)

        await _fed(stt, Transcript(text="Moment"))

        assert sock.events("clear") == []
        assert "CA_ms" in channel._active_turns
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


class TestShutdown:
    async def test_shutdown_clears_the_queued_reply_before_the_goodbye(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        epoch = state.metadata.get("speech_epoch", 0)

        await channel.stop()

        kinds = [m.get("event") for m in sock.sent]
        assert "clear" in kinds
        # the goodbye's audio comes after the clear, not queued behind the old reply
        assert "media" in kinds[kinds.index("clear") :]
        assert state.metadata["speech_epoch"] > epoch
        assert OLD_NEVER not in engine._tts_provider.synthesized
        assert engine.metrics_registry.get("CA_ms").barge_in_reactions_ms == []

    async def test_shutdown_hangs_up_only_after_the_goodbye_played_out(self, monkeypatch):
        """send_speech returns once the goodbye is AT Twilio, which is faster
        than real time; the hangup has to wait until the caller heard it."""
        from pincer.voice.telemetry.clock import mono_ns

        monkeypatch.setattr(MediaStreamEngine, "wait_for_playout", _REAL_WAIT_FOR_PLAYOUT)
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        engine._tts_provider.chunks = 1  # a one-second goodbye
        hung_up_at: list[int] = []
        end_call = engine.end_call

        async def _timed_end_call(call_sid: str) -> None:
            hung_up_at.append(mono_ns())
            await end_call(call_sid)

        monkeypatch.setattr(engine, "end_call", _timed_end_call)

        await channel.stop()

        kinds = [m.get("event") for m in sock.sent]
        assert "media" in kinds[kinds.index("clear") :]  # the goodbye went out
        assert hung_up_at and hung_up_at[0] >= state.metadata["playout_until_ns"]

    async def test_our_own_cut_is_not_a_caller_interruption(self):
        engine, channel, agent, sock, stt, state = await _agent_mid_reply()
        from pincer.voice.analytics import get_accumulator

        accumulator = get_accumulator(state)
        before = accumulator.interruptions if accumulator is not None else 0

        await engine.interrupt_speech("CA_ms", by_caller=False)

        assert len(sock.events("clear")) == 1
        if accumulator is not None:
            assert accumulator.interruptions == before
        await channel.stop()


class TestPartialSegments:
    """A segment cut mid-stream: logged as cut off, and still billed."""

    async def test_a_turn_cancelled_mid_sentence_logs_it_as_interrupted(self):
        tts = FakeTTS(chunks=20, delay_s=0.05)  # still synthesizing when the caller cuts in
        engine = _engine(tts, "interim")
        channel, agent = await _channel_for(engine, [text_chunks(OLD_FIRST), text_chunks(NEW_REPLY)])
        sock, stt = await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        await _fed(stt, _final("Erste Frage?"))
        await sock.wait_sent(lambda sent: any(m.get("event") == "media" for m in sent))
        turn = channel._active_turns["CA_ms"]
        # began before our audio: not an echo of it
        await _fed(stt, Transcript(text="Moment", words=[TranscriptWord("Moment", start=0.0, end=0.1)]))
        await asyncio.wait_for(asyncio.wait([turn]), timeout=2)

        assert turn.cancelled()

        line = next(e for e in channel.get_transcript("CA_ms").entries if e.text == OLD_FIRST)
        assert line.state == "interrupted"
        assert state.metadata["tts_characters"] == len(OLD_FIRST)
        tts.delay_s = 0  # the shutdown goodbye need not take real time
        await channel.stop()

    async def test_a_turn_cancelled_before_any_audio_logs_it_as_undelivered(self):
        """Cancelled while still waiting for the first TTS chunk: the caller
        heard none of it, so it must not be credited as cut off part-way."""
        tts = FakeTTS(chunks=20, delay_s=0.05)
        tts.hold_first = asyncio.Event()  # the first synthesis produces nothing until released
        engine = _engine(tts, "off")
        channel, _agent = await _channel_for(engine, [text_chunks(OLD_FIRST), text_chunks(NEW_REPLY)])
        sock, stt = await _call(engine, "CA_ms")

        await _fed(stt, _final("Erste Frage?"))
        await asyncio.wait_for(tts.started.wait(), timeout=2)
        turn = channel._active_turns["CA_ms"]
        await _fed(stt, _final("Moment, etwas anderes!"))  # cancels the old turn before any media
        tts.hold_first.set()
        await channel.wait_for_turn("CA_ms")

        assert turn.cancelled()
        assert sock.events("media") and tts.synthesized[-1] == NEW_REPLY
        line = next(e for e in channel.get_transcript("CA_ms").entries if e.text == OLD_FIRST)
        assert line.state == "undelivered"
        tts.delay_s = 0
        await channel.stop()

    async def test_the_cut_sentence_is_logged_before_the_interrupting_caller_line(self):
        """The caller line is logged before the old turn unwinds; the cut
        sentence must still land before it, in the reply it belongs to."""
        tts = FakeTTS(chunks=20, delay_s=0.05)
        engine = _engine(tts, "off")
        channel, _agent = await _channel_for(engine, [text_chunks(OLD_FIRST), text_chunks(NEW_REPLY)])
        sock, stt = await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        await _fed(stt, _final("Erste Frage?"))
        await sock.wait_sent(lambda sent: any(m.get("event") == "media" for m in sent))
        state.metadata["playout_started_ns"] -= (BARGE_IN_ARM_DELAY_MS + 50) * 1_000_000  # not our echo
        await _fed(stt, _final("Moment, etwas anderes!"))
        await channel.wait_for_turn("CA_ms")

        lines = [(e.speaker, e.text) for e in channel.get_transcript("CA_ms").entries]
        assert lines == [
            ("caller", "Erste Frage?"),
            ("agent", OLD_FIRST),
            ("caller", "Moment, etwas anderes!"),
            ("agent", NEW_REPLY),
        ]
        states = [e.state for e in channel.get_transcript("CA_ms").entries if e.speaker == "agent"]
        assert states == ["interrupted", "intent_capture"]
        tts.delay_s = 0
        await channel.stop()

    async def test_speech_cut_by_a_barge_in_reports_the_cut_and_is_billed(self):
        engine = _engine(FakeTTS(chunks=20, delay_s=0.05))
        sock, _stt = await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        speaking = asyncio.create_task(engine.send_speech("CA_ms", OLD_FIRST))
        await sock.wait_sent(lambda sent: any(m.get("event") == "media" for m in sent))
        await engine.interrupt_speech("CA_ms")

        assert await speaking is True  # some audio reached Twilio
        assert state.metadata["last_speech_cut"] is True
        assert state.metadata["tts_characters"] == len(OLD_FIRST)

    async def test_uncut_speech_is_not_reported_as_cut(self):
        engine = _engine(FakeTTS())
        await _call(engine, "CA_ms")
        state = engine.get_call_state("CA_ms")

        assert await engine.send_speech("CA_ms", OLD_FIRST) is True
        assert state.metadata["last_speech_cut"] is False
        assert state.metadata["tts_characters"] == len(OLD_FIRST)


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
