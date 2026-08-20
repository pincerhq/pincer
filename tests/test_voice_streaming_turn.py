"""Streaming voice pipeline tests (Sprint 5, T5.2/T5.7) — channel level.

A fake stream agent stands in for the LLM; the FakeVoiceEngine records what
the caller would hear and in what order. The acceptance-critical properties:
first audio before generation ends, clean barge-in cancellation, and the
language/appointment guards staying closed under streaming.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import StreamChunk, StreamEventType
from pincer.voice import scheduling
from pincer.voice.engine import CallDirection
from pincer.voice.prompts import de as de_pack
from pincer.voice.transcript import Speaker


def _settings():
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_filler_phrases = ""
    settings.data_dir = None  # no jsonl writes in unit tests
    return settings


class FakeStreamAgent:
    """Yields a scripted sequence of StreamChunks per turn, optionally
    pausing so tests can observe mid-generation state."""

    def __init__(self, scripts: list[list[StreamChunk]], chunk_delay_s: float = 0.0) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict] = []
        self.pause_after: int | None = None  # chunk index to pause at
        self.resume = asyncio.Event()
        self.paused = asyncio.Event()
        # Real generation takes time. A latency assertion against a fully
        # synchronous fake compares two numbers that are both inside one
        # rounding step, which is noise rather than a measurement — tests that
        # care about ordering set a small delay so the stages actually separate.
        self.chunk_delay_s = chunk_delay_s

    async def stream_voice_turn(self, **kwargs):
        self.calls.append(kwargs)
        script = self.scripts.pop(0) if self.scripts else []
        for index, chunk in enumerate(script):
            if self.pause_after is not None and index == self.pause_after:
                self.paused.set()
                await self.resume.wait()
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            yield chunk


def text_chunks(*tokens: str) -> list[StreamChunk]:
    chunks = [StreamChunk(StreamEventType.TEXT, t) for t in tokens]
    chunks.append(StreamChunk(StreamEventType.DONE, "".join(tokens)))
    return chunks


async def _start(agent_scripts, language="de", call_sid="CA_stream"):
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    stream_agent = FakeStreamAgent(agent_scripts)
    channel.set_stream_agent(stream_agent)

    async def _blocking_handler(incoming):
        return "Entschuldigung, hier ist die korrigierte Antwort auf Deutsch für Sie."

    await channel.start(_blocking_handler)
    state = await engine.on_call_start(call_sid, "+4930123456", CallDirection.INBOUND, language=language)
    return channel, engine, stream_agent, state


@pytest.fixture(autouse=True)
def _clean_scheduling():
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


class TestSentenceStreaming:
    async def test_sentences_streamed_with_last_flags(self):
        script = text_chunks("Gerne, das mache ich sofort. ", "Welche Uhrzeit passt Ihnen denn am besten?")
        channel, engine, agent, state = await _start([script])

        await engine.on_speech_input("CA_stream", "Bitte einen Termin machen.")

        assert engine.spoken["CA_stream"] == [
            "Gerne, das mache ich sofort.",
            "Welche Uhrzeit passt Ihnen denn am besten?",
        ]
        await channel.stop()

    async def test_first_audio_before_generation_ends(self):
        """Acceptance: t_tts_first_chunk < t_llm_done — the first sentence is
        already spoken while the 'LLM' is paused mid-generation."""
        script = text_chunks("Einen Moment bitte, ich prüfe das. ", "Das dauert nur kurz, versprochen.")
        channel, engine, agent, state = await _start([script])
        agent.pause_after = 1  # pause after the first TEXT chunk was consumed

        turn = asyncio.create_task(engine.on_speech_input("CA_stream", "Hallo?"))
        await asyncio.wait_for(agent.paused.wait(), timeout=2)

        # Generation is paused — the first sentence must already be out
        assert engine.spoken["CA_stream"] == ["Einen Moment bitte, ich prüfe das."]

        agent.resume.set()
        await asyncio.wait_for(turn, timeout=2)
        assert engine.spoken["CA_stream"][-1] == "Das dauert nur kurz, versprochen."
        await channel.stop()

    async def test_transcript_records_streamed_sentences(self):
        script = text_chunks("Erster Satz ist hier fertig. ", "Zweiter Satz kommt gleich danach.")
        channel, engine, agent, state = await _start([script])
        await engine.on_speech_input("CA_stream", "Hallo.")
        transcript = channel.get_transcript("CA_stream")
        agent_lines = [e.text for e in transcript.entries if e.speaker == Speaker.AGENT]
        assert agent_lines == ["Erster Satz ist hier fertig.", "Zweiter Satz kommt gleich danach."]
        await channel.stop()

    async def test_voice_context_passed_to_stream_agent(self):
        channel, engine, agent, state = await _start([text_chunks("Guten Tag, womit kann ich helfen?")])
        await engine.on_speech_input("CA_stream", "Hallo.")
        extra = agent.calls[0]["extra_system"]
        assert de_pack.VOICE_SYSTEM_PROMPT in extra
        assert de_pack.LANGUAGE_POLICY in extra
        await channel.stop()


class TestBargeIn:
    async def test_barge_in_cancels_streamed_turn(self):
        """A new caller utterance mid-stream cancels the running turn; no
        queued sentences from the old turn play afterwards."""
        slow_script = text_chunks("Der erste Satz der alten Antwort. ", "Dieser Satz darf niemals gesprochen werden.")
        next_script = text_chunks("Die neue Antwort auf die Unterbrechung.")
        channel, engine, agent, state = await _start([slow_script, next_script])
        agent.pause_after = 1  # old turn stalls after sentence 1

        old_turn = asyncio.create_task(engine.on_speech_input("CA_stream", "Erste Frage?"))
        await asyncio.wait_for(agent.paused.wait(), timeout=2)
        assert engine.spoken["CA_stream"] == ["Der erste Satz der alten Antwort."]

        # Barge-in: the caller speaks again while the old turn is mid-stream
        # (the second turn must run without the pause hook)
        agent.pause_after = None
        await asyncio.wait_for(engine.on_speech_input("CA_stream", "Moment, etwas anderes!"), timeout=2)
        await asyncio.wait_for(old_turn, timeout=2)

        spoken = engine.spoken["CA_stream"]
        assert "Dieser Satz darf niemals gesprochen werden." not in spoken
        assert spoken[-1] == "Die neue Antwort auf die Unterbrechung."
        assert engine.interrupts.get("CA_stream", 0) >= 1
        await channel.stop()


class TestGuardsUnderStreaming:
    async def test_drift_first_sentence_buffers_and_regenerates(self):
        """English drift on a German call: nothing of the drifted turn is
        spoken; the buffered turn goes through the blocking guard path."""
        drifted = text_chunks("Sure, I can help you with that right away. ", "What time works for you today?")
        channel, engine, agent, state = await _start([drifted])

        await engine.on_speech_input("CA_stream", "Können Sie mir helfen?")

        spoken = engine.spoken["CA_stream"]
        assert all("Sure, I can help" not in s for s in spoken)
        assert all("What time works" not in s for s in spoken)
        # The corrected block (from the blocking regeneration handler) played
        assert spoken == ["Entschuldigung, hier ist die korrigierte Antwort auf Deutsch für Sie."]
        await channel.stop()

    async def test_switch_token_in_first_sentence_switches_and_strips(self):
        script = text_chunks("[SWITCH_LANGUAGE:de] Gerne, dann sprechen wir Deutsch. ", "Womit kann ich helfen?")
        channel, engine, agent, state = await _start([script], language="en")

        await engine.on_speech_input("CA_stream", "Können wir Deutsch sprechen? Ja bitte.")

        assert state.language == "de"
        spoken = engine.spoken["CA_stream"]
        assert all("[SWITCH_LANGUAGE" not in s for s in spoken)
        assert spoken[0].startswith("Gerne, dann sprechen wir Deutsch.")
        await channel.stop()

    async def test_unsupported_switch_declined_and_rest_suppressed(self):
        script = text_chunks("[SWITCH_LANGUAGE:fr] Bien sûr, on continue. ", "Je peux vous aider avec tout.")
        channel, engine, agent, state = await _start([script], language="en")

        await engine.on_speech_input("CA_stream", "Can we speak French please?")

        from pincer.voice.prompts import en as en_pack

        assert engine.spoken["CA_stream"] == [en_pack.LANGUAGE_SWITCH_UNSUPPORTED]
        assert state.language == "en"
        await channel.stop()

    async def test_out_of_slot_appointment_confirmation_never_streams(self):
        task = scheduling.AppointmentTask(
            task_id="t1",
            user_id="u1",
            channel="telegram",
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Termin",
            timeframe="next_week",
            duration_minutes=30,
            language="de",
            candidates=["2026-08-25T14:00:00+02:00"],
        )
        scheduling.register_appointment("CA_stream", task)
        script = text_chunks(
            "[APPOINTMENT_CONFIRMED:2026-08-27T18:00:00+02:00] Abgemacht, Donnerstag 18 Uhr passt. ",
            "Ich trage das sofort für Sie ein.",
        )
        channel, engine, agent, state = await _start([script])

        await engine.on_speech_input("CA_stream", "Nur Donnerstag 18 Uhr geht.")

        spoken = engine.spoken["CA_stream"]
        assert len(spoken) == 1
        assert "Abgemacht" not in spoken[0]
        assert "trage das sofort" not in " ".join(spoken)  # rest suppressed
        assert task.status == "out_of_candidates"
        await channel.stop()

    async def test_valid_appointment_confirmation_streams_stripped(self):
        task = scheduling.AppointmentTask(
            task_id="t1",
            user_id="u1",
            channel="telegram",
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Termin",
            timeframe="next_week",
            duration_minutes=30,
            language="de",
            candidates=["2026-08-25T14:00:00+02:00"],
        )
        scheduling.register_appointment("CA_stream", task)
        script = text_chunks(
            "[APPOINTMENT_CONFIRMED:2026-08-25T14:00:00+02:00] Wunderbar, dann bis Dienstag um vierzehn Uhr. ",
            "Auf Wiederhören und danke!",
        )
        channel, engine, agent, state = await _start([script])

        await engine.on_speech_input("CA_stream", "Ja, Dienstag 14 Uhr passt.")

        assert task.status == "confirmed"
        spoken = engine.spoken["CA_stream"]
        assert spoken[0].startswith("Wunderbar")
        assert all("[APPOINTMENT_CONFIRMED" not in s for s in spoken)
        await channel.stop()


class TestFillerOnToolStart:
    async def test_filler_spoken_instantly_on_tool_start(self):
        """T5.7 / Sprint 11 §6.1: the caller hears an acknowledgment before a
        slow tool runs, before any LLM text exists. With the in-call gate the
        filler is the gate's localized TOOL_WAIT_FILLER (slow tools only)."""
        from pincer.voice.in_call_tools import current_gate

        class GateAwareAgent:
            """Routes the tool through the bound in-call gate like the real agent."""

            async def stream_voice_turn(self, **kwargs):
                gate = current_gate()
                assert gate is not None
                yield StreamChunk(StreamEventType.TOOL_START, "google__list_events")

                async def _exec():
                    return "No events between 2026-08-19 and 2026-08-19.", False

                await gate.run("google__list_events", {"time_min": "", "time_max": ""}, _exec)
                yield StreamChunk(StreamEventType.TOOL_DONE, "google__list_events")
                yield StreamChunk(StreamEventType.TEXT, "Morgen stehen keine Termine an. ")
                yield StreamChunk(StreamEventType.DONE, "Morgen stehen keine Termine an.")

        channel, engine, agent, state = await _start([[]])
        channel.set_stream_agent(GateAwareAgent())

        await engine.on_speech_input("CA_stream", "Was steht morgen an?")

        spoken = engine.spoken["CA_stream"]
        assert len(spoken) == 2
        assert spoken[0] == de_pack.TOOL_WAIT_FILLER  # instant, localized filler first
        assert spoken[1] == "Morgen stehen keine Termine an."
        await channel.stop()

    async def test_no_filler_when_text_already_spoken(self):
        script = [
            StreamChunk(StreamEventType.TEXT, "Einen kleinen Moment bitte, ich schaue nach. "),
            StreamChunk(StreamEventType.TOOL_START, "calendar_today"),
            StreamChunk(StreamEventType.TOOL_DONE, "calendar_today"),
            StreamChunk(StreamEventType.TEXT, "Morgen sind zwei Termine im Kalender. "),
            StreamChunk(StreamEventType.DONE, "..."),
        ]
        channel, engine, agent, state = await _start([script])
        await engine.on_speech_input("CA_stream", "Was steht morgen an?")
        spoken = engine.spoken["CA_stream"]
        assert spoken[0] == "Einen kleinen Moment bitte, ich schaue nach."
        assert all(s not in de_pack.FILLER_PHRASES for s in spoken)
        await channel.stop()


class TestLatencyInstrumentation:
    async def test_turn_latency_jsonl_written(self, tmp_path):
        from voice_harness.fake_engine import FakeVoiceEngine

        settings = _settings()
        settings.data_dir = tmp_path
        engine = FakeVoiceEngine(settings)
        channel = VoiceChannel(settings)
        channel.set_engine(engine)
        # Multi-token script with a per-chunk delay: the sentence completes (and
        # is dispatched) while the model is still "generating", which is the
        # behaviour this test exists to assert.
        channel.set_stream_agent(
            FakeStreamAgent(
                [text_chunks("Gerne, ", "das mache ich ", "sofort für Sie. ", "Sonst noch etwas?")],
                chunk_delay_s=0.01,
            )
        )

        async def _handler(incoming):
            return ""

        await channel.start(_handler)
        await engine.on_call_start("CA_lat", "+4930123456", CallDirection.INBOUND, language="de")
        await engine.on_speech_input("CA_lat", "Hallo!")

        import json

        path = tmp_path / "logs" / "voice_latency.jsonl"
        assert path.is_file()
        record = json.loads(path.read_text().strip().splitlines()[-1])
        assert record["call_sid"] == "CA_lat"
        assert record["turn"] == 1
        assert record["streamed"] is True
        assert "total_ms" in record
        assert "llm_first_token_ms" in record
        assert "first_dispatch_ms" in record
        # Streaming acceptance: first audio dispatched before the LLM finished.
        # With the per-chunk delay above the gap is milliseconds, not a rounding
        # step, so this compares a real ordering.
        assert record["first_dispatch_ms"] < record["llm_done_ms"]
        await channel.stop()
