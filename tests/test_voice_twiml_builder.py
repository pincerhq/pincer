"""Tests for the shared TwiML builder + ConversationRelay voice config (Sprint 4)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pincer.voice import voices
from pincer.voice.language import (
    ELEVENLABS_DEFAULT_VOICE_ID,
    cr_tts_provider,
    elevenlabs_voice_for,
    relay_voice_attr,
    voice_for,
)
from pincer.voice.twiml_builder import build_connect_twiml


def _settings(**kwargs):
    defaults = {
        "voice_engine": "conversation_relay",
        "voice_webhook_base_url": "https://example.com",
        "voice_default_language": "en",
        "voice_supported_languages": "en,de,uk",
        "voice_language": "en-US",
        "voice_de_formality": "sie",
        "voice_consent_mode": "none",
        "voice_recording_enabled": False,
        "voice_consent_language": "",
        "voice_assistant_name": "",
        "voice_assistant_org": "",
        "voice_assistant_owner": "",
        "voice_intro_text": "",
        "elevenlabs_voice_id": "",
        "elevenlabs_voice_id_en": "",
        "elevenlabs_voice_id_de": "",
        "elevenlabs_voice_id_uk": "",
        "elevenlabs_model": "eleven_flash_v2_5",
        "cr_tts_provider": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clean_validation_cache():
    voices._reset_validation_cache_for_tests()
    yield
    voices._reset_validation_cache_for_tests()


class TestVoiceResolver:
    """T4.3: per-call override > language-specific > global > documented default."""

    def test_override_wins(self):
        s = _settings(elevenlabs_voice_id="glob", elevenlabs_voice_id_de="de-voice")
        assert voice_for(s, "de", override="per-call") == "per-call"

    def test_language_specific_beats_global(self):
        s = _settings(elevenlabs_voice_id="glob", elevenlabs_voice_id_de="de-voice")
        assert voice_for(s, "de") == "de-voice"
        assert voice_for(s, "en") == "glob"

    def test_documented_default_when_nothing_configured(self):
        assert voice_for(_settings(), "en") == ELEVENLABS_DEFAULT_VOICE_ID

    def test_elevenlabs_voice_for_stays_empty_without_config(self):
        assert elevenlabs_voice_for(_settings(), "en") == ""


class TestCRTTSProvider:
    def test_auto_google_without_voice(self):
        assert cr_tts_provider(_settings(), "en") == "google"

    def test_auto_elevenlabs_with_voice(self):
        assert cr_tts_provider(_settings(elevenlabs_voice_id="v1"), "en") == "elevenlabs"

    def test_explicit_setting_wins(self):
        s = _settings(elevenlabs_voice_id="v1", cr_tts_provider="google")
        assert cr_tts_provider(s, "en") == "google"

    def test_unknown_setting_falls_back_to_auto(self):
        assert cr_tts_provider(_settings(cr_tts_provider="polly"), "en") == "google"

    def test_relay_voice_attr_model_suffix(self):
        s = _settings(elevenlabs_voice_id="v1", elevenlabs_model="eleven_turbo_v2_5")
        assert relay_voice_attr(s, "en") == "v1-turbo_v2_5"
        # default model needs no suffix (Twilio default is flash_v2_5 too)
        assert relay_voice_attr(_settings(elevenlabs_voice_id="v1"), "en") == "v1"


class TestConversationRelayTwiml:
    def test_elevenlabs_voice_used_when_configured(self):
        """Acceptance: configured ElevenLabs voice on CR with zero code edits."""
        s = _settings(elevenlabs_voice_id="MyClonedVoice")
        for direction in ("inbound", "outbound"):
            twiml = build_connect_twiml(s, call_sid="CA1", direction=direction, language="en", counterparty="+1555")
            assert 'ttsProvider="ElevenLabs"' in twiml
            assert 'voice="MyClonedVoice"' in twiml
            assert 'language="en-US"' in twiml
            assert 'url="wss://example.com/api/apps/twilio/relay"' in twiml

    def test_relay_url_is_websocket(self):
        """Hotfix: ConversationRelay is WebSocket-only — an https url is
        rejected by Twilio with error 64101 ("application error" after the
        greeting). The url attribute must always be wss://."""
        for s in (_settings(), _settings(elevenlabs_voice_id="v1")):
            for direction in ("inbound", "outbound"):
                twiml = build_connect_twiml(s, call_sid="CA1", direction=direction, language="en", counterparty="+1")
                start = twiml.index('<ConversationRelay url="') + len('<ConversationRelay url="')
                url = twiml[start : twiml.index('"', start)]
                assert url.startswith("wss://"), url

    def test_provider_and_voice_never_mixed(self):
        """Regression guard: ttsProvider and voice are resolved together —
        an ElevenLabs provider never carries a Google voice name or vice versa."""
        combos = [
            _settings(),
            _settings(elevenlabs_voice_id="v1"),
            _settings(elevenlabs_voice_id_de="de-v"),
            _settings(cr_tts_provider="elevenlabs"),  # provider forced, no voice configured
            _settings(cr_tts_provider="google", elevenlabs_voice_id="v1"),
        ]
        for s in combos:
            for lang in ("en", "de"):
                twiml = build_connect_twiml(s, direction="outbound", language=lang, counterparty="+1")
                if 'ttsProvider="ElevenLabs"' in twiml:
                    assert 'voice="Google.' not in twiml, twiml
                else:
                    assert 'ttsProvider="Google"' in twiml
                    assert 'voice="Google.' in twiml, twiml

    def test_google_fallback_without_elevenlabs_voice(self):
        twiml = build_connect_twiml(_settings(), direction="inbound", language="en", counterparty="+1555")
        assert 'ttsProvider="Google"' in twiml
        assert 'voice="Google.en-US-Neural2-F"' in twiml

    def test_german_call_uses_german_voice(self):
        s = _settings(elevenlabs_voice_id_en="en-v", elevenlabs_voice_id_de="de-v")
        twiml = build_connect_twiml(s, direction="outbound", language="de", counterparty="+49555")
        assert 'voice="de-v"' in twiml
        assert 'language="de-DE"' in twiml

    def test_ukrainian_call_uses_ukrainian_voice(self):
        s = _settings(
            elevenlabs_voice_id_en="en-v",
            elevenlabs_voice_id_uk="uk-v",
            voice_assistant_name="Pincer",
        )
        twiml = build_connect_twiml(s, direction="outbound", language="uk", counterparty="+380555")
        assert 'voice="uk-v"' in twiml
        assert 'language="uk-UA"' in twiml
        assert "Це Pincer" in twiml  # Ukrainian welcomeGreeting

    def test_invalid_voice_falls_back_to_twilio_default(self, caplog):
        """T4.5/Hotfix 3: an unusable voice (startup validation or live 64111)
        → the voice attribute is omitted so Twilio applies its documented
        per-language default ElevenLabs voice — call still happens."""
        s = _settings(elevenlabs_voice_id="dead-voice")
        voices._invalid_voice_ids.add("dead-voice")
        twiml = build_connect_twiml(s, direction="inbound", language="uk", counterparty="+380555")
        assert 'ttsProvider="ElevenLabs"' in twiml
        assert "dead-voice" not in twiml
        assert "voice=" not in twiml
        assert 'language="uk-UA"' in twiml
        assert any("unusable" in r.message for r in caplog.records)

    def test_no_say_preroll_on_conversation_relay(self):
        """The robotic <Say> greeting is gone on CR: the opening moves into
        welcomeGreeting and is spoken by the relay's own configured voice."""
        s = _settings(voice_assistant_name="Pincer", voice_assistant_org="3days.ai")
        for direction in ("inbound", "outbound"):
            twiml = build_connect_twiml(s, direction=direction, language="en", counterparty="+1555")
            assert "<Say" not in twiml
            assert 'welcomeGreeting="This is Pincer, the AI assistant from 3days.ai."' in twiml

    def test_no_welcome_greeting_when_intro_disabled(self):
        # Empty assistant name + consent mode none = truly no greeting
        twiml = build_connect_twiml(_settings(), direction="outbound", language="en", counterparty="+1555")
        assert "welcomeGreeting" not in twiml
        assert "<Say" not in twiml

    def test_welcome_greeting_german(self):
        s = _settings(voice_assistant_name="Pincer", voice_assistant_org="3days.ai")
        twiml = build_connect_twiml(s, direction="outbound", language="de", counterparty="+49555")
        assert "welcomeGreeting=" in twiml
        assert "Hier spricht Pincer" in twiml

    def test_welcome_greeting_quotes_escaped(self):
        s = _settings(voice_intro_text='Hi, I am "Pincer" & friends <ok>')
        twiml = build_connect_twiml(s, direction="outbound", language="en", counterparty="+1555")
        assert 'welcomeGreeting="Hi, I am &quot;Pincer&quot; &amp; friends &lt;ok&gt;"' in twiml

    def test_xml_declaration_present(self):
        twiml = build_connect_twiml(_settings(), direction="outbound", language="en", counterparty="+1555")
        assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?><Response>')
        assert twiml.endswith("</Response>")


class TestMediaStreamsTwiml:
    def test_inbound_stream_with_sid_and_status_callback(self):
        s = _settings(voice_engine="media_streams")
        twiml = build_connect_twiml(s, call_sid="CA42", direction="inbound", language="en", counterparty="+1555")
        assert '<Stream url="wss://example.com/api/apps/twilio/stream/CA42"' in twiml
        assert 'statusCallbackUrl="https://example.com/api/apps/twilio/status"' in twiml
        # media_streams keeps the spoken connect line (stream setup takes a moment)
        assert "connect you to your assistant" in twiml

    def test_outbound_stream_placeholder(self):
        s = _settings(voice_engine="media_streams")
        twiml = build_connect_twiml(s, direction="outbound", language="en", counterparty="+1555")
        assert "wss://example.com/api/apps/twilio/stream/{CallSid}" in twiml
        assert "statusCallbackUrl" not in twiml

    def test_consent_precedes_connect(self):
        s = _settings(
            voice_engine="media_streams",
            voice_consent_mode="two_party",
            voice_recording_enabled=True,
        )
        twiml = build_connect_twiml(s, call_sid="CA1", direction="outbound", language="de", counterparty="+49555")
        assert "aufgezeichnet" in twiml
        assert twiml.index("aufgezeichnet") < twiml.index("<Connect>")
