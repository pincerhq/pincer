"""Tests for per-call language resolution and prompt i18n (Sprint 2)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from pincer.voice.language import (
    elevenlabs_model_for,
    elevenlabs_voice_for,
    relay_language,
    relay_voice,
    resolve_call_language,
    stt_language,
    supported_languages,
)
from pincer.voice.prompts import get_filler_phrases, get_prompt
from pincer.voice.stt import stt_config_for_language


def _settings(**kwargs):
    defaults = {
        "voice_default_language": "en",
        "voice_supported_languages": "en,de,uk",
        "voice_language": "en-US",
        "voice_de_formality": "sie",
        "elevenlabs_voice_id": "generic",
        "elevenlabs_voice_id_en": "",
        "elevenlabs_voice_id_de": "",
        "elevenlabs_voice_id_uk": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestResolveCallLanguage:
    def test_explicit_request_wins(self):
        assert resolve_call_language(_settings(), "de") == "de"

    def test_default_when_no_request(self):
        assert resolve_call_language(_settings(voice_default_language="de")) == "de"

    def test_unsupported_request_falls_back(self):
        assert resolve_call_language(_settings(), "fr") == "en"

    def test_region_suffix_normalized(self):
        assert resolve_call_language(_settings(), "de-DE") == "de"

    def test_legacy_voice_language_fallback(self):
        settings = _settings(voice_default_language="", voice_language="de-DE")
        assert resolve_call_language(settings) == "de"

    def test_supported_list_restricts(self):
        settings = _settings(voice_supported_languages="en")
        assert resolve_call_language(settings, "de") == "en"

    def test_magicmock_settings_survive(self):
        # Defensive: unset/mocked settings must still resolve to a real language
        assert resolve_call_language(MagicMock(), "de") == "de"
        assert resolve_call_language(MagicMock()) in ("en", "de")

    def test_supported_parse_garbage(self):
        assert supported_languages(_settings(voice_supported_languages="xx,,yy")) == ["en", "de", "uk"]

    def test_ukrainian_resolves_when_supported(self):
        settings = _settings(voice_supported_languages="en,de,uk")
        assert resolve_call_language(settings, "uk") == "uk"
        assert resolve_call_language(settings, "uk-UA") == "uk"

    def test_ukrainian_restricted_when_not_listed(self):
        assert resolve_call_language(_settings(voice_supported_languages="en,de"), "uk") == "en"


class TestProviderMappings:
    def test_relay_mappings(self):
        assert relay_language("de") == "de-DE"
        assert relay_language("en") == "en-US"
        assert relay_language("uk") == "uk-UA"
        assert "de-DE" in relay_voice("de")
        assert "uk-UA" in relay_voice("uk")
        assert relay_language("unknown") == "en-US"

    def test_stt_config_german_endpointing(self):
        config = stt_config_for_language("de")
        assert config.language == "de"
        assert config.endpointing > stt_config_for_language("en").endpointing
        assert config.utterance_end_ms > stt_config_for_language("en").utterance_end_ms

    def test_stt_language(self):
        assert stt_language("de-DE") == "de"

    def test_elevenlabs_model_configurable(self):
        # Sprint 4: one multilingual model for every language, settings-driven
        assert elevenlabs_model_for(_settings(), "en") == "eleven_flash_v2_5"
        assert elevenlabs_model_for(_settings(), "de") == "eleven_flash_v2_5"
        custom = _settings(elevenlabs_model="eleven_multilingual_v2")
        assert elevenlabs_model_for(custom, "de") == "eleven_multilingual_v2"
        assert elevenlabs_model_for(_settings(elevenlabs_model="garbage")) == "eleven_flash_v2_5"
        assert elevenlabs_model_for(MagicMock()) == "eleven_flash_v2_5"

    def test_elevenlabs_voice_per_language(self):
        settings = _settings(elevenlabs_voice_id_de="voice-de")
        assert elevenlabs_voice_for(settings, "de") == "voice-de"
        assert elevenlabs_voice_for(settings, "en") == "generic"  # falls back

    def test_elevenlabs_voice_ukrainian(self):
        settings = _settings(elevenlabs_voice_id_uk="voice-uk")
        assert elevenlabs_voice_for(settings, "uk") == "voice-uk"
        assert elevenlabs_voice_for(settings, "uk-UA") == "voice-uk"


class TestPromptI18n:
    def test_german_system_prompt(self):
        prompt = get_prompt("VOICE_SYSTEM_PROMPT", "de")
        assert "Deutsch" in prompt
        assert "Siezen" in prompt

    def test_du_formality_override(self):
        prompt = get_prompt("VOICE_SYSTEM_PROMPT", "de", formality="du")
        assert "Duzen" in prompt

    def test_missing_key_falls_back_to_english(self):
        assert get_prompt("VOICE_VERIFY_PROMPT", "de") is not None
        assert get_prompt("LOW_CONFIDENCE_REPLY", "xx") == get_prompt("LOW_CONFIDENCE_REPLY", "en")

    def test_german_fillers(self):
        phrases = get_filler_phrases("de")
        assert any("Moment" in p for p in phrases)

    def test_backward_compatible_exports(self):
        from pincer.voice.prompts import FILLER_PHRASES, LOW_CONFIDENCE_REPLY, VOICE_SYSTEM_PROMPT

        assert "phone call" in VOICE_SYSTEM_PROMPT
        assert LOW_CONFIDENCE_REPLY
        assert FILLER_PHRASES

    def test_phase_dicts_localized(self):
        from pincer.voice.state_machine import CallStateMachine

        sm = CallStateMachine("CA1")
        sm.start_call()
        assert "Begrüßen" in sm.get_phase_instruction("de")
        assert "Greet" in sm.get_phase_instruction("en")
        assert "Wiederhören" in sm.get_timeout_message("de")
        assert "oodbye" in sm.get_timeout_message("en")

    def test_every_english_timeout_has_german_counterpart(self):
        en_messages = get_prompt("PHASE_TIMEOUT_MESSAGES", "en")
        de_messages = get_prompt("PHASE_TIMEOUT_MESSAGES", "de")
        assert set(de_messages) == set(en_messages)

    def test_every_english_instruction_has_german_counterpart(self):
        assert set(get_prompt("PHASE_INSTRUCTIONS", "de")) == set(get_prompt("PHASE_INSTRUCTIONS", "en"))

    def test_ukrainian_prompts(self):
        prompt = get_prompt("VOICE_SYSTEM_PROMPT", "uk")
        assert "українською" in prompt
        assert "Перепрошую" in get_prompt("LOW_CONFIDENCE_REPLY", "uk")
        assert any("перевірю" in p.lower() for p in get_filler_phrases("uk"))

    def test_ukrainian_phase_dicts_complete(self):
        assert set(get_prompt("PHASE_TIMEOUT_MESSAGES", "uk")) == set(get_prompt("PHASE_TIMEOUT_MESSAGES", "en"))
        assert set(get_prompt("PHASE_INSTRUCTIONS", "uk")) == set(get_prompt("PHASE_INSTRUCTIONS", "en"))

    def test_ukrainian_state_machine_strings(self):
        from pincer.voice.state_machine import CallStateMachine

        sm = CallStateMachine("CA_uk")
        sm.start_call()
        assert "українською" in sm.get_phase_instruction("uk")
        assert "побачення" in sm.get_timeout_message("uk")


class TestConsentByCallLanguage:
    def test_outbound_consent_uses_call_language(self):
        from pincer.voice.compliance import build_consent_say_twiml

        settings = MagicMock()
        settings.voice_consent_mode = "two_party"
        settings.voice_recording_enabled = True
        settings.voice_consent_language = ""
        settings.voice_language = "en-US"
        settings.voice_assistant_name = "Pincer"
        settings.voice_assistant_org = "3days.ai"
        settings.voice_assistant_owner = ""
        settings.voice_intro_text = ""
        # US number, but the call is German → German announcement
        twiml = build_consent_say_twiml(settings, "+14155551234", language="de")
        assert 'language="de-DE"' in twiml
        assert "aufgezeichnet" in twiml

    def test_ukrainian_consent_announcement(self):
        from pincer.voice.compliance import build_consent_say_twiml

        settings = MagicMock()
        settings.voice_consent_mode = "two_party"
        settings.voice_recording_enabled = True
        settings.voice_consent_language = ""
        settings.voice_language = "en-US"
        settings.voice_assistant_name = "Pincer"
        settings.voice_assistant_org = "3days.ai"
        settings.voice_assistant_owner = ""
        settings.voice_intro_text = ""
        twiml = build_consent_say_twiml(settings, "+380501234567", language="uk")
        assert 'language="uk-UA"' in twiml
        assert "запис" in twiml  # consent announcement
        assert "Це Pincer" in twiml  # Ukrainian introduction


class TestGermanOutboundTwiml:
    async def test_language_de_produces_german_call(self, monkeypatch):
        """make_phone_call(language='de') → German consent + de-DE relay attributes."""
        from pincer.voice import outbound

        settings = MagicMock()
        settings.voice_enabled = True
        settings.voice_outbound_enabled = True
        settings.voice_webhook_base_url = "https://example.com"
        settings.voice_outbound_max_daily = 10
        settings.voice_max_call_duration = 600
        settings.voice_engine = "conversation_relay"
        settings.voice_language = "en-US"
        settings.voice_default_language = "en"
        settings.voice_supported_languages = "en,de"
        settings.voice_machine_detection = False
        settings.voice_consent_mode = "two_party"
        settings.voice_recording_enabled = True
        settings.voice_consent_language = ""
        settings.voice_assistant_name = "Pincer"
        settings.voice_assistant_org = "3days.ai"
        settings.voice_assistant_owner = ""
        settings.voice_intro_text = ""
        settings.twilio_account_sid = "AC123"
        settings.twilio_auth_token.get_secret_value.return_value = "token"
        settings.twilio_phone_number = "+4915212345678"
        monkeypatch.setattr("pincer.config.get_settings", lambda: settings)

        captured: dict = {}

        class FakeCalls:
            def create(self, **kwargs):
                captured.update(kwargs)
                call = MagicMock()
                call.sid = "CA_de"
                return call

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.calls = FakeCalls()

        import sys
        import types

        twilio_rest = types.ModuleType("twilio.rest")
        twilio_rest.Client = FakeClient
        twilio_mod = types.ModuleType("twilio")
        twilio_mod.rest = twilio_rest
        monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
        monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)

        result = await outbound.make_phone_call(
            "+491761234567",
            "Zahnarzttermin bestätigen",
            language="de",
            context={"user_id": "u1", "channel": "telegram"},
        )
        assert "Call initiated" in result

        twiml = captured["twiml"]
        assert 'language="de-DE"' in twiml  # relay + consent both German
        assert 'voice="Google.de-DE-Neural2-F"' in twiml
        assert "aufgezeichnet" in twiml  # German consent announcement
        assert "Hier spricht Pincer" in twiml  # German introduction

        # The tracked call carries its language for downstream registration
        from pincer.voice.status_notify import get_call_language

        assert get_call_language("CA_de") == "de"
