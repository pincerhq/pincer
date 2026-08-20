"""Language consistency on voice calls — channel-level scenarios.

The rule under test: a call has exactly one active language, pinned in
CallState.language. The agent speaks only that language; it switches only via
the explicit, confirmed [SWITCH_LANGUAGE:xx] flow that updates every layer at
once. Runs against the real VoiceChannel + FakeVoiceEngine with scripted
handlers (no LLM), harness-style.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import Agent
from pincer.voice.engine import CallDirection
from pincer.voice.prompts import de as de_pack
from pincer.voice.prompts import en as en_pack
from pincer.voice.stt import stt_config_for_language
from pincer.voice.transcript import Speaker

if TYPE_CHECKING:
    from pincer.channels.base import IncomingMessage


def _settings():
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    return settings


class ScriptedHandler:
    """Handler returning canned responses in order; records what it was asked."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.received: list[IncomingMessage] = []

    async def __call__(self, incoming: IncomingMessage) -> str:
        self.received.append(incoming)
        return self.responses.pop(0) if self.responses else "Okay."


async def _start_call(handler, language: str = "de", call_sid: str = "CA_lang"):
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    await channel.start(handler)
    state = await engine.on_call_start(call_sid, "+15550001111", CallDirection.INBOUND, language=language)
    return channel, engine, state


# ── T2: single-pack prompt injection (marker test) ───────────────────


class TestSinglePackContext:
    async def test_german_turn_context_has_no_english_fragments(self):
        """Assemble a full German turn context (system rules + language policy
        + phase instruction) and assert it contains nothing from the en pack."""
        handler = ScriptedHandler(["Gerne, womit kann ich Ihnen helfen?"])
        channel, engine, state = await _start_call(handler, language="de")
        await engine.on_speech_input("CA_lang", "Guten Tag, ich brauche einen Termin.")

        extra = handler.received[0].extra_system
        assert extra, "voice turns must carry the call-language system context"

        # The full de pack is present…
        assert de_pack.VOICE_SYSTEM_PROMPT in extra
        assert de_pack.LANGUAGE_POLICY in extra
        # …and no en-pack fragment leaked in.
        en_fragments = [
            en_pack.VOICE_SYSTEM_PROMPT,
            en_pack.LANGUAGE_POLICY,
            *en_pack.PHASE_INSTRUCTIONS.values(),
        ]
        for fragment in en_fragments:
            assert fragment not in extra, f"en-pack fragment leaked into de call context: {fragment[:60]!r}"
        await channel.stop()

    async def test_english_turn_context_uses_en_pack(self):
        handler = ScriptedHandler(["Sure, what can I do for you?"])
        channel, engine, state = await _start_call(handler, language="en")
        await engine.on_speech_input("CA_lang", "Hi, I need an appointment.")

        extra = handler.received[0].extra_system
        assert en_pack.VOICE_SYSTEM_PROMPT in extra
        assert en_pack.LANGUAGE_POLICY in extra
        assert de_pack.VOICE_SYSTEM_PROMPT not in extra
        await channel.stop()

    async def test_phase_instruction_follows_call_language(self):
        handler = ScriptedHandler(["Gerne."])
        channel, engine, state = await _start_call(handler, language="de")
        await engine.on_speech_input("CA_lang", "Hallo.")
        # After caller speaks, phase is INTENT_CAPTURE — its de instruction is included
        assert de_pack.PHASE_INSTRUCTIONS["intent_capture"] in handler.received[0].extra_system
        await channel.stop()


# ── Scenario 1: code-switching callee never flips the agent ──────────


class TestNoUnrequestedSwitch:
    async def test_german_call_survives_english_interjection(self):
        """The callee throws in one English sentence; the scripted agent
        answers in German — nothing regenerates, nothing switches."""
        handler = ScriptedHandler(
            [
                "Gerne, welche Uhrzeit passt Ihnen denn am Dienstag?",
                "Alles klar, dann trage ich Dienstag vierzehn Uhr für Sie ein. Passt das so?",
            ]
        )
        channel, engine, state = await _start_call(handler, language="de")

        await engine.on_speech_input("CA_lang", "Ich hätte gerne einen Termin am Dienstag.")
        await engine.on_speech_input("CA_lang", "Oh sorry, I mean Tuesday at two pm please, thank you!")

        assert state.language == "de"
        assert len(handler.received) == 2  # no regeneration happened
        assert engine.spoken["CA_lang"] == [
            "Gerne, welche Uhrzeit passt Ihnen denn am Dienstag?",
            "Alles klar, dann trage ich Dienstag vierzehn Uhr für Sie ein. Passt das so?",
        ]
        await channel.stop()

    async def test_drifted_response_is_regenerated_in_call_language(self):
        """LLM mirrors the callee into English on a German call: the guard
        requests one regeneration with the de corrective note and speaks the
        fixed reply. The wrong-language draft is never spoken."""
        drifted = "Sure, I can help you with that. What time works for you tomorrow?"
        fixed = "Gerne, welche Uhrzeit passt Ihnen denn morgen für den Termin?"
        handler = ScriptedHandler([drifted, fixed])
        channel, engine, state = await _start_call(handler, language="de")

        await engine.on_speech_input("CA_lang", "Können Sie mir bitte einen Termin machen?")

        assert engine.spoken["CA_lang"] == [fixed]
        assert state.language == "de"
        # The regeneration turn carried the corrective note from the de pack
        assert len(handler.received) == 2
        assert handler.received[1].text == de_pack.LANGUAGE_REGEN_NOTE
        assert handler.received[1].extra_system == handler.received[0].extra_system
        await channel.stop()

    async def test_persistent_drift_never_blocks_the_call(self):
        """If the regeneration also mismatches, the reply is spoken anyway —
        exactly one regeneration, never silence."""
        drifted = "Sure, I can help you with that. What time works for you tomorrow?"
        still_wrong = "I am still answering in English even after the note, sorry about that."
        handler = ScriptedHandler([drifted, still_wrong])
        channel, engine, state = await _start_call(handler, language="de")

        await engine.on_speech_input("CA_lang", "Können Sie mir bitte einen Termin machen?")

        assert engine.spoken["CA_lang"] == [still_wrong]
        assert len(handler.received) == 2  # one regeneration, not a loop
        await channel.stop()


# ── Scenario 2: explicit, confirmed switch ───────────────────────────


class TestExplicitSwitch:
    async def test_confirmed_switch_flows_through_all_layers(self):
        """English call; callee asks for German; agent confirms; after the
        clear yes the agent emits the switch token. The call switches and
        STAYS German; the transcript shows the SYSTEM audit entry."""
        handler = ScriptedHandler(
            [
                "You'd like to continue in German, is that right?",
                "[SWITCH_LANGUAGE:de] Gerne! Womit kann ich Ihnen weiterhelfen?",
                "Sehr gerne, ich kümmere mich darum. Einen Moment bitte.",
            ]
        )
        channel, engine, state = await _start_call(handler, language="en")

        await engine.on_speech_input("CA_lang", "Können wir auf Deutsch weitermachen, bitte?")
        assert state.language == "en"  # confirmation question does not switch yet

        await engine.on_speech_input("CA_lang", "Ja, bitte.")
        assert state.language == "de"
        assert engine.spoken["CA_lang"][1] == "Gerne! Womit kann ich Ihnen weiterhelfen?"
        assert "[SWITCH_LANGUAGE" not in " ".join(engine.spoken["CA_lang"])

        # Audit trail in the transcript
        transcript = channel.get_transcript("CA_lang")
        system_entries = [e for e in transcript.entries if e.speaker == Speaker.SYSTEM]
        assert any("language switched from en to de" in e.text for e in system_entries)

        # The switch sticks: the next turn is assembled from the de pack
        await engine.on_speech_input("CA_lang", "Bitte buchen Sie den Termin am Dienstag.")
        assert de_pack.VOICE_SYSTEM_PROMPT in handler.received[2].extra_system
        assert state.language == "de"
        await channel.stop()

    async def test_unsupported_switch_politely_declined(self):
        handler = ScriptedHandler(["[SWITCH_LANGUAGE:fr] Bien sûr, continuons en français."])
        channel, engine, state = await _start_call(handler, language="en")

        await engine.on_speech_input("CA_lang", "Can we continue in French please?")

        assert state.language == "en"
        assert engine.spoken["CA_lang"] == [en_pack.LANGUAGE_SWITCH_UNSUPPORTED]
        await channel.stop()


# ── T3 regression: STT pinned to the call language ───────────────────


class TestSTTLanguagePinned:
    def test_de_call_never_opens_en_stt(self):
        config = stt_config_for_language("de")
        assert config.language == "de"
        assert stt_config_for_language("en").language == "en"
        assert stt_config_for_language("uk").language == "uk"

    async def test_media_stream_stt_uses_state_language(self, monkeypatch):
        """A de call opens a de STT stream (regression for leak 4)."""
        from unittest.mock import AsyncMock

        from pincer.voice.engine import MediaStreamEngine

        settings = _settings()
        engine = MediaStreamEngine(settings)
        configs: list = []

        class _FakeStream:
            async def receive_transcripts(self):
                return
                yield  # pragma: no cover

            async def close(self):
                pass

            async def send_audio(self, _b):
                pass

        provider = MagicMock()

        async def _start_stream(config):
            configs.append(config)
            return _FakeStream()

        provider.start_stream = AsyncMock(side_effect=_start_stream)
        engine._stt_provider = provider

        await engine._register_call("CA_stt", "+1555", CallDirection.INBOUND, language="de")
        await engine.setup_media_stream_stt("CA_stt", "MS1")

        assert configs and configs[0].language == "de"
        await engine.close_media_stream("CA_stt")


# ── Agent plumbing: extra_system reaches the LLM system prompt ───────


class TestAgentExtraSystem:
    async def test_extra_system_lands_in_llm_system_prompt(
        self, settings, mock_llm, session_manager, cost_tracker, tool_registry
    ):
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
        await agent.handle_message(
            user_id="u1",
            channel="voice",
            text="hello",
            extra_system="VOICE-RULES-MARKER-12345",
        )
        system = mock_llm.complete.call_args.kwargs["system"]
        assert "VOICE-RULES-MARKER-12345" in system

    async def test_no_extra_system_unchanged(self, settings, mock_llm, session_manager, cost_tracker, tool_registry):
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
        await agent.handle_message(user_id="u1", channel="voice", text="hello")
        system = mock_llm.complete.call_args.kwargs["system"]
        assert "VOICE-RULES-MARKER" not in system


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
