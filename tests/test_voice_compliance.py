"""Tests for recording consent and compliance."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pincer.voice.compliance import (
    ComplianceChecker,
    ConsentMode,
    build_call_opening,
    build_consent_say_twiml,
    build_intro_text,
    detect_jurisdiction,
    get_ai_disclosure,
    get_consent_announcement,
    get_consent_mode,
    resolve_consent_language,
    should_record,
)


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.voice_consent_mode = "one_party"
    settings.voice_recording_enabled = True
    return settings


class TestDetectJurisdiction:
    def test_california(self):
        assert detect_jurisdiction("+14155551234") == "US-two-party"

    def test_us_one_party(self):
        assert detect_jurisdiction("+12125551234") == "US-one-party"

    def test_germany(self):
        assert detect_jurisdiction("+491761234567") == "DE"

    def test_uk(self):
        assert detect_jurisdiction("+442012345678") == "UK"

    def test_unknown(self):
        assert detect_jurisdiction("+81312345678") == "unknown"


class TestGetConsentMode:
    def test_configured_none(self, mock_settings):
        mock_settings.voice_consent_mode = "none"
        assert get_consent_mode(mock_settings, "+1234") == ConsentMode.NONE

    def test_configured_two_party(self, mock_settings):
        mock_settings.voice_consent_mode = "two_party"
        assert get_consent_mode(mock_settings, "+1234") == ConsentMode.TWO_PARTY

    def test_auto_detect_california(self, mock_settings):
        mock_settings.voice_consent_mode = "one_party"
        assert get_consent_mode(mock_settings, "+14155551234") == ConsentMode.TWO_PARTY

    def test_auto_detect_germany(self, mock_settings):
        mock_settings.voice_consent_mode = "one_party"
        assert get_consent_mode(mock_settings, "+491761234567") == ConsentMode.TWO_PARTY


class TestConsentAnnouncement:
    def test_one_party_english(self):
        text = get_consent_announcement(ConsentMode.ONE_PARTY)
        assert text is not None
        assert "recorded" in text

    def test_two_party_english(self):
        text = get_consent_announcement(ConsentMode.TWO_PARTY)
        assert text is not None
        assert "consent" in text

    def test_german_jurisdiction(self):
        text = get_consent_announcement(ConsentMode.ONE_PARTY, "+491761234567")
        assert text is not None
        assert "aufgezeichnet" in text

    def test_none_returns_nothing(self):
        assert get_consent_announcement(ConsentMode.NONE) is None


class TestShouldRecord:
    def test_recording_disabled(self, mock_settings):
        mock_settings.voice_recording_enabled = False
        assert not should_record(mock_settings, True)

    def test_recording_with_consent(self, mock_settings):
        assert should_record(mock_settings, True)

    def test_recording_without_consent(self, mock_settings):
        assert not should_record(mock_settings, False)


class TestComplianceChecker:
    def test_inbound_check(self, mock_settings):
        checker = ComplianceChecker(mock_settings)
        result = checker.check_inbound_call("+14155551234")
        assert result.jurisdiction == "US-two-party"
        assert result.mode == ConsentMode.TWO_PARTY

    def test_outbound_check(self, mock_settings):
        checker = ComplianceChecker(mock_settings)
        result = checker.check_outbound_call("+442012345678")
        assert result.jurisdiction == "UK"
        assert not result.consent_given


# ── Sprint 0: DACH consent language & announcements ───────────────────────────


class TestDachJurisdictions:
    def test_austria(self):
        assert detect_jurisdiction("+436601234567") == "AT"

    def test_switzerland(self):
        assert detect_jurisdiction("+41791234567") == "CH"

    def test_switzerland_auto_two_party(self, mock_settings):
        mock_settings.voice_consent_mode = "one_party"
        assert get_consent_mode(mock_settings, "+41791234567") == ConsentMode.TWO_PARTY

    def test_austria_stays_one_party(self, mock_settings):
        mock_settings.voice_consent_mode = "one_party"
        assert get_consent_mode(mock_settings, "+436601234567") == ConsentMode.ONE_PARTY


class TestResolveConsentLanguage:
    def _settings(self, consent_language="", voice_language="en-US"):
        settings = MagicMock()
        settings.voice_consent_language = consent_language
        settings.voice_language = voice_language
        return settings

    def test_explicit_setting_wins(self):
        settings = self._settings(consent_language="de", voice_language="en-US")
        assert resolve_consent_language(settings, "+14155551234") == "de"

    def test_follows_call_language(self):
        settings = self._settings(voice_language="de-DE")
        assert resolve_consent_language(settings, "+14155551234") == "de"

    def test_german_jurisdiction_fallback(self):
        settings = self._settings()
        assert resolve_consent_language(settings, "+491761234567") == "de"

    def test_austrian_jurisdiction_fallback(self):
        settings = self._settings()
        assert resolve_consent_language(settings, "+436601234567") == "de"

    def test_default_english(self):
        settings = self._settings()
        assert resolve_consent_language(settings, "+14155551234") == "en"


class TestGermanAnnouncements:
    def test_two_party_german(self):
        text = get_consent_announcement(ConsentMode.TWO_PARTY, language="de")
        assert text is not None
        assert "aufgezeichnet" in text
        assert "stimmen Sie" in text  # consent clause

    def test_one_party_german(self):
        text = get_consent_announcement(ConsentMode.ONE_PARTY, language="de")
        assert text is not None
        assert "aufgezeichnet" in text

    def test_language_overrides_jurisdiction(self):
        text = get_consent_announcement(ConsentMode.TWO_PARTY, "+491761234567", language="en")
        assert text is not None
        assert "consent" in text

    def test_no_recording_german_ai_disclosure(self):
        text = get_consent_announcement(ConsentMode.TWO_PARTY, language="de", recording=False)
        assert text is not None
        assert "KI-Assistenten" in text

    def test_no_recording_english_ai_disclosure(self):
        text = get_consent_announcement(ConsentMode.TWO_PARTY, language="en", recording=False)
        assert text is not None
        assert "AI assistant" in text

    def test_no_recording_mode_none_stays_silent(self):
        assert get_consent_announcement(ConsentMode.NONE, language="de", recording=False) is None


class TestAiDisclosure:
    def test_german(self):
        assert "KI-Assistenten" in get_ai_disclosure("de")

    def test_english_default(self):
        assert "AI assistant" in get_ai_disclosure("en")


class TestInboundCheckDach:
    def test_german_caller_gets_announcement(self):
        settings = MagicMock()
        settings.voice_consent_mode = "one_party"
        settings.voice_recording_enabled = True
        settings.voice_consent_language = ""
        settings.voice_language = "en-US"
        checker = ComplianceChecker(settings)
        result = checker.check_inbound_call("+491761234567")
        assert result.jurisdiction == "DE"
        assert result.mode == ConsentMode.TWO_PARTY
        assert result.announcement_played

    def test_disclosure_played_even_without_recording(self):
        settings = MagicMock()
        settings.voice_consent_mode = "two_party"
        settings.voice_recording_enabled = False
        settings.voice_consent_language = "de"
        settings.voice_language = "de-DE"
        checker = ComplianceChecker(settings)
        result = checker.check_inbound_call("+491761234567")
        assert result.announcement_played


def _twiml_settings(
    mode="two_party",
    recording=True,
    consent_language="",
    voice_language="en-US",
    name="Pincer",
    org="3days.ai",
    owner="",
    intro_text="",
):
    settings = MagicMock()
    settings.voice_consent_mode = mode
    settings.voice_recording_enabled = recording
    settings.voice_consent_language = consent_language
    settings.voice_language = voice_language
    settings.voice_assistant_name = name
    settings.voice_assistant_org = org
    settings.voice_assistant_owner = owner
    settings.voice_intro_text = intro_text
    return settings


class TestBuildConsentSayTwiml:
    def test_german_two_party(self):
        twiml = build_consent_say_twiml(_twiml_settings(consent_language="de"), "+491761234567")
        assert twiml.startswith('<Say language="de-DE">')
        assert "stimmen Sie der Aufzeichnung zu" in twiml

    def test_english_two_party(self):
        twiml = build_consent_say_twiml(_twiml_settings(), "+12125551234")
        assert twiml.startswith('<Say language="en-US">')
        assert "consent" in twiml

    def test_ai_disclosure_without_recording_and_no_intro(self):
        settings = _twiml_settings(recording=False, consent_language="de", name="")
        twiml = build_consent_say_twiml(settings, "+491761234567")
        assert "KI-Assistenten" in twiml

    def test_mode_none_without_intro_is_empty(self):
        assert build_consent_say_twiml(_twiml_settings(mode="none", name=""), "+491761234567") == ""

    def test_intro_precedes_consent(self):
        twiml = build_consent_say_twiml(_twiml_settings(owner="Volodymyr Pugachov"), "+12125551234")
        intro_pos = twiml.index("This is Pincer")
        consent_pos = twiml.index("consent")
        assert intro_pos < consent_pos


class TestBuildIntroText:
    def test_full_introduction_english(self):
        settings = _twiml_settings(owner="Volodymyr Pugachov")
        text = build_intro_text(settings, "en")
        assert text == "This is Pincer, the AI assistant from 3days.ai and personal AI assistant of Volodymyr Pugachov."

    def test_full_introduction_german(self):
        settings = _twiml_settings(owner="Volodymyr Pugachov")
        text = build_intro_text(settings, "de")
        assert text == (
            "Hier spricht Pincer, der KI-Assistent von 3days.ai und persönlicher KI-Assistent von Volodymyr Pugachov."
        )

    def test_settings_are_changeable(self):
        settings = _twiml_settings(name="Jarvis", org="Acme GmbH", owner="Jane Doe")
        text = build_intro_text(settings, "en")
        assert text == "This is Jarvis, the AI assistant from Acme GmbH and personal AI assistant of Jane Doe."

    def test_owner_only(self):
        settings = _twiml_settings(org="", owner="Jane Doe")
        assert build_intro_text(settings, "en") == "This is Pincer, the personal AI assistant of Jane Doe."

    def test_name_only(self):
        settings = _twiml_settings(org="", owner="")
        assert build_intro_text(settings, "en") == "This is Pincer."

    def test_empty_name_disables_intro(self):
        assert build_intro_text(_twiml_settings(name=""), "en") == ""

    def test_verbatim_override(self):
        settings = _twiml_settings(intro_text="Hello, this is a custom greeting.")
        assert build_intro_text(settings, "de") == "Hello, this is a custom greeting."


class TestBuildCallOpening:
    def test_intro_plus_consent_when_recording(self):
        opening = build_call_opening(
            _twiml_settings(consent_language="de", owner="Volodymyr Pugachov"), "+491761234567"
        )
        assert opening.startswith("Hier spricht Pincer")
        assert "stimmen Sie der Aufzeichnung zu" in opening

    def test_intro_replaces_ai_disclosure_when_not_recording(self):
        opening = build_call_opening(_twiml_settings(recording=False), "+12125551234")
        assert opening == "This is Pincer, the AI assistant from 3days.ai."

    def test_intro_plays_even_with_consent_mode_none(self):
        opening = build_call_opening(_twiml_settings(mode="none"), "+12125551234")
        assert opening == "This is Pincer, the AI assistant from 3days.ai."

    def test_silent_when_intro_disabled_and_mode_none(self):
        assert build_call_opening(_twiml_settings(mode="none", name=""), "+12125551234") == ""

    def test_bare_name_intro_keeps_ai_disclosure(self):
        # "This is Pincer." mentions no AI, so the disclosure must still play.
        opening = build_call_opening(_twiml_settings(recording=False, org="", owner=""), "+12125551234")
        assert opening == "This is Pincer. Please note: you are speaking with an automated AI assistant."

    def test_override_without_ai_mention_keeps_ai_disclosure(self):
        settings = _twiml_settings(recording=False, intro_text="Hi, this is Pincer calling on behalf of Jane.")
        opening = build_call_opening(settings, "+12125551234")
        assert opening == (
            "Hi, this is Pincer calling on behalf of Jane. "
            "Please note: you are speaking with an automated AI assistant."
        )

    def test_override_with_ai_mention_suppresses_disclosure(self):
        settings = _twiml_settings(recording=False, intro_text="Hi, this is Pincer, Jane's AI assistant.")
        opening = build_call_opening(settings, "+12125551234")
        assert opening == "Hi, this is Pincer, Jane's AI assistant."

    def test_german_intro_suppresses_disclosure(self):
        opening = build_call_opening(_twiml_settings(recording=False, consent_language="de"), "+491761234567")
        assert opening == "Hier spricht Pincer, der KI-Assistent von 3days.ai."
