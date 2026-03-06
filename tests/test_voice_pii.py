"""Tests for PII masking in transcripts and logs."""

from __future__ import annotations

import pytest

from pincer.voice.pii_guard import (
    contains_pii,
    mask_dtmf_input,
    mask_pii,
    sanitize_for_logs,
)


class TestMaskPII:
    def test_credit_card_visa(self):
        result = mask_pii("My card number is 4111 1111 1111 1111")
        assert "4111 **** **** 1111" in result
        assert "1111 1111 1111" not in result

    def test_credit_card_dashes(self):
        result = mask_pii("Card: 4111-1111-1111-1111")
        assert "****" in result

    def test_ssn(self):
        result = mask_pii("My SSN is 123-45-6789")
        assert "[SSN_REDACTED]" in result
        assert "123-45-6789" not in result

    def test_ssn_no_dashes(self):
        result = mask_pii("SSN 123456789 is mine")
        assert "[SSN_REDACTED]" in result

    def test_pin_in_text(self):
        result = mask_pii("My PIN is 4567")
        assert "[PIN_REDACTED]" in result
        assert "4567" not in result

    def test_account_number(self):
        result = mask_pii("account number 12345678901")
        assert "[ACCOUNT_REDACTED]" in result

    def test_no_pii_unchanged(self):
        text = "Hello, I would like to reschedule my appointment."
        assert mask_pii(text) == text

    def test_multiple_pii(self):
        text = "SSN 123-45-6789 and PIN: 1234"
        result = mask_pii(text)
        assert "[SSN_REDACTED]" in result
        assert "[PIN_REDACTED]" in result


class TestContainsPII:
    def test_credit_card_detected(self):
        assert contains_pii("4111 1111 1111 1111")

    def test_ssn_detected(self):
        assert contains_pii("123-45-6789")

    def test_clean_text(self):
        assert not contains_pii("Hello, how are you today?")


class TestMaskDTMF:
    def test_short_input(self):
        assert mask_dtmf_input("12") == "12"

    def test_pin_length(self):
        result = mask_dtmf_input("1234")
        assert result == "1**4"

    def test_longer_input(self):
        result = mask_dtmf_input("123456")
        assert result.startswith("1")
        assert result.endswith("6")
        assert "****" in result


class TestSanitizeForLogs:
    def test_email_redacted(self):
        result = sanitize_for_logs("Email me at john@example.com")
        assert "[EMAIL_REDACTED]" in result
        assert "john@example.com" not in result

    def test_combined(self):
        text = "SSN 123-45-6789, email test@test.com, DOB 01/15/1990"
        result = sanitize_for_logs(text)
        assert "[SSN_REDACTED]" in result
        assert "[EMAIL_REDACTED]" in result
        assert "[DOB_REDACTED]" in result
