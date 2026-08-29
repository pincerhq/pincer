"""Unit tests for voice/language_guard.py — detection, switch token, check_and_fix."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.voice.engine import CallDirection, CallState
from pincer.voice.language_guard import (
    check_and_fix,
    detect_language,
    parse_switch_token,
    perform_switch,
)
from pincer.voice.transcript import Speaker, TranscriptLogger


def _settings(**overrides):
    values = {
        "voice_supported_languages": "en,de,uk",
        "voice_default_language": "en",
        "voice_de_formality": "sie",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _state(language: str = "en", metadata: dict | None = None) -> CallState:
    state = CallState(
        call_sid="CA_guard",
        direction=CallDirection.INBOUND,
        caller_number="+15550001111",
        language=language,
    )
    if metadata:
        state.metadata.update(metadata)
    return state


def _fake_engine(name: str = "fake"):
    engine = MagicMock()
    engine.engine_name = name
    del engine.setup_media_stream_stt  # plain engines have no MS STT hook
    return engine


# ── detect_language ──────────────────────────────────────────────────


class TestDetectLanguage:
    @pytest.mark.parametrize(
        "text",
        [
            "Sure, I can help you with that. What time works for you tomorrow?",
            "I have booked the appointment for you, is there anything else you need?",
            "Thank you for calling, please let me know what you would like to do.",
            "That is not a problem at all, we can do it on Tuesday if you like.",
        ],
    )
    def test_clear_english(self, text):
        assert detect_language(text) == "en"

    @pytest.mark.parametrize(
        "text",
        [
            "Gerne, ich kann Ihnen dabei helfen. Welche Uhrzeit passt Ihnen morgen?",
            "Ich habe den Termin für Sie eingetragen. Kann ich sonst noch etwas für Sie tun?",
            "Vielen Dank für Ihren Anruf, bitte sagen Sie mir, was Sie möchten.",
            "Das ist überhaupt kein Problem, wir können das gerne am Dienstag machen.",
        ],
    )
    def test_clear_german(self, text):
        assert detect_language(text) == "de"

    def test_clear_ukrainian_by_script(self):
        assert detect_language("Гаразд, я запишу вас на вівторок о третій годині. Щось іще?") == "uk"

    @pytest.mark.parametrize(
        "text",
        [
            "Okay",  # short confirmation
            "Ja, genau",  # short
            "Dr. Müller, Hauptstraße 12, München",  # proper nouns only
            "Meeting Termin Update Service Manager",  # borrowed words, no stopwords
            "",
        ],
    )
    def test_short_or_no_signal_is_ambiguous(self, text):
        assert detect_language(text) is None

    def test_borrowed_english_words_in_german_stay_german(self):
        # "Meeting" and "Update" must not drag a German sentence to English
        text = "Ich habe das Meeting verschoben und ein Update für Sie, passt Ihnen das so?"
        assert detect_language(text) == "de"

    def test_german_name_in_english_stays_english(self):
        text = "I booked the Termin with Doctor Müller for you, is that okay with you?"
        assert detect_language(text) == "en"

    def test_mixed_language_line_is_ambiguous(self):
        assert detect_language("Das ist okay, we can do that for sure now") is None

    def test_short_cyrillic_is_ambiguous(self):
        assert detect_language("Так, добре") is None


# ── parse_switch_token ───────────────────────────────────────────────


class TestParseSwitchToken:
    def test_no_token(self):
        assert parse_switch_token("Hello there") == (None, "Hello there")

    def test_leading_token(self):
        code, rest = parse_switch_token("[SWITCH_LANGUAGE:de] Gerne, wir sprechen jetzt Deutsch.")
        assert code == "de"
        assert rest == "Gerne, wir sprechen jetzt Deutsch."

    def test_token_case_and_spaces(self):
        code, rest = parse_switch_token("[SWITCH_LANGUAGE: EN ] Sure, switching to English.")
        assert code == "en"
        assert rest == "Sure, switching to English."

    def test_unsupported_code_is_returned(self):
        code, _ = parse_switch_token("[SWITCH_LANGUAGE:fr] Bien sûr.")
        assert code == "fr"

    def test_all_tokens_stripped(self):
        _, rest = parse_switch_token("[SWITCH_LANGUAGE:de] Hallo [SWITCH_LANGUAGE:de]")
        assert "[SWITCH_LANGUAGE" not in rest


# ── perform_switch ───────────────────────────────────────────────────


class TestPerformSwitch:
    async def test_conversation_relay_sends_language_message(self):
        ws = MagicMock()
        ws.send_text = AsyncMock()
        state = _state("en", metadata={"websocket": ws})
        engine = _fake_engine("conversation_relay")
        transcript = TranscriptLogger("CA_guard")

        await perform_switch(state, "de", engine=engine, settings=_settings(), transcript=transcript)

        assert state.language == "de"
        sent = json.loads(ws.send_text.call_args.args[0])
        assert sent == {"type": "language", "ttsLanguage": "de-DE", "transcriptionLanguage": "de-DE"}
        system_entries = [e for e in transcript.entries if e.speaker == Speaker.SYSTEM]
        assert len(system_entries) == 1
        assert "language switched from en to de" in system_entries[0].text

    async def test_media_streams_reopens_stt(self):
        engine = MagicMock()
        engine.engine_name = "media_streams"
        engine.close_media_stream = AsyncMock()
        engine.setup_media_stream_stt = AsyncMock()
        state = _state("en", metadata={"stt_stream": object(), "stream_sid": "MS123"})

        await perform_switch(state, "de", engine=engine, settings=_settings())

        assert state.language == "de"
        engine.close_media_stream.assert_awaited_once_with("CA_guard")
        engine.setup_media_stream_stt.assert_awaited_once_with("CA_guard", "MS123")

    async def test_cr_without_websocket_still_switches_state(self, caplog):
        state = _state("en")
        engine = _fake_engine("conversation_relay")
        with caplog.at_level("WARNING"):
            await perform_switch(state, "de", engine=engine, settings=_settings())
        assert state.language == "de"
        assert any("transcription stays" in r.message for r in caplog.records)


# ── check_and_fix ────────────────────────────────────────────────────


class TestCheckAndFix:
    async def test_happy_path_untouched_no_regen(self):
        state = _state("en")
        regen = AsyncMock()
        text, switched = await check_and_fix(
            "Sure, I can help you with that. What time works for you?",
            state,
            engine=_fake_engine(),
            settings=_settings(),
            regenerate=regen,
        )
        assert text.startswith("Sure, I can help")
        assert switched is False
        regen.assert_not_awaited()

    async def test_ambiguous_short_reply_untouched(self):
        state = _state("de")
        regen = AsyncMock()
        text, switched = await check_and_fix(
            "Okay!", state, engine=_fake_engine(), settings=_settings(), regenerate=regen
        )
        assert (text, switched) == ("Okay!", False)
        regen.assert_not_awaited()

    async def test_switch_token_switches_and_strips(self):
        ws = MagicMock()
        ws.send_text = AsyncMock()
        state = _state("en", metadata={"websocket": ws})
        transcript = TranscriptLogger("CA_guard")

        text, switched = await check_and_fix(
            "[SWITCH_LANGUAGE:de] Gerne, dann sprechen wir jetzt Deutsch. Womit kann ich helfen?",
            state,
            engine=_fake_engine("conversation_relay"),
            settings=_settings(),
            transcript=transcript,
        )
        assert switched is True
        assert state.language == "de"
        assert "[SWITCH_LANGUAGE" not in text
        assert text.startswith("Gerne")
        assert any(e.speaker == Speaker.SYSTEM for e in transcript.entries)

    async def test_switch_token_with_no_text_falls_back_to_ack(self):
        state = _state("en")
        text, switched = await check_and_fix("[SWITCH_LANGUAGE:de]", state, engine=_fake_engine(), settings=_settings())
        assert switched is True
        assert state.language == "de"
        assert text  # the localized ack line, never empty speech

    async def test_switch_token_to_current_language_is_stripped_noop(self):
        state = _state("de")
        text, switched = await check_and_fix(
            "[SWITCH_LANGUAGE:de] Wir sprechen bereits Deutsch.",
            state,
            engine=_fake_engine(),
            settings=_settings(),
        )
        assert switched is False
        assert state.language == "de"
        assert text == "Wir sprechen bereits Deutsch."

    async def test_unsupported_language_token_declines_in_current_language(self):
        state = _state("de")
        text, switched = await check_and_fix(
            "[SWITCH_LANGUAGE:fr] Bien sûr, on continue en français.",
            state,
            engine=_fake_engine(),
            settings=_settings(),
        )
        assert switched is False
        assert state.language == "de"
        assert "[SWITCH_LANGUAGE" not in text
        assert "Deutsch" in text  # polite decline from the de pack

    async def test_clear_mismatch_regenerates_once(self):
        state = _state("de")
        notes: list[str] = []

        async def regen(note: str) -> str:
            notes.append(note)
            return "Entschuldigung, gerne noch einmal auf Deutsch: Ihr Termin ist am Dienstag. Passt das für Sie?"

        text, switched = await check_and_fix(
            "Sure, I can help you with that. What time works for you tomorrow?",
            state,
            engine=_fake_engine(),
            settings=_settings(),
            regenerate=regen,
        )
        assert switched is False
        assert len(notes) == 1
        assert "Deutsch" in notes[0]  # corrective note comes from the call-language pack
        assert text.startswith("Entschuldigung, gerne noch einmal")

    async def test_persistent_mismatch_still_sends(self, caplog):
        state = _state("de")

        async def regen(note: str) -> str:
            return "I am still answering in English even after the note, sorry about that."

        with caplog.at_level("ERROR"):
            text, switched = await check_and_fix(
                "Sure, I can help you with that. What time works for you tomorrow?",
                state,
                engine=_fake_engine(),
                settings=_settings(),
                regenerate=regen,
            )
        assert switched is False
        assert text.startswith("I am still answering")  # never block the call
        assert any("persists" in r.message for r in caplog.records)

    async def test_regen_failure_sends_original(self):
        state = _state("de")

        async def regen(note: str) -> str:
            raise RuntimeError("boom")

        original = "Sure, I can help you with that. What time works for you tomorrow?"
        text, switched = await check_and_fix(
            original, state, engine=_fake_engine(), settings=_settings(), regenerate=regen
        )
        assert (text, switched) == (original, False)

    async def test_regen_never_switches_language(self):
        state = _state("de")

        async def regen(note: str) -> str:
            return "[SWITCH_LANGUAGE:en] Fine, let's talk English then."

        text, switched = await check_and_fix(
            "Sure, I can help you with that. What time works for you tomorrow?",
            state,
            engine=_fake_engine(),
            settings=_settings(),
            regenerate=regen,
        )
        assert switched is False
        assert state.language == "de"  # drift correction must not initiate a switch
        assert "[SWITCH_LANGUAGE" not in text

    async def test_no_regen_callback_sends_original(self):
        state = _state("de")
        original = "Sure, I can help you with that. What time works for you tomorrow?"
        text, switched = await check_and_fix(original, state, engine=_fake_engine(), settings=_settings())
        assert (text, switched) == (original, False)
