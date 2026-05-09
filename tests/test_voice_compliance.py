"""Tests for recording consent and compliance."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pincer.voice.compliance import (
    ComplianceChecker,
    ConsentMode,
    detect_jurisdiction,
    get_consent_announcement,
    get_consent_mode,
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
