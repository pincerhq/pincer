"""Tests for safety gates and confirmation parsing."""

from __future__ import annotations

import pytest

from pincer.voice.safety_gates import (
    ActionCategory,
    ConfirmationStatus,
    build_confirmation_prompt,
    classify_action,
    create_gate,
    parse_confirmation,
    requires_confirmation,
)


class TestParseConfirmation:
    @pytest.mark.parametrize(
        "utterance",
        [
            "yes",
            "Yeah",
            "yep",
            "sure",
            "go ahead",
            "do it",
            "correct",
            "confirmed",
            "absolutely",
            "OK",
            "okay",
            "sounds good",
            "perfect",
            "go for it",
            "please do",
        ],
    )
    def test_affirmative(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.CONFIRMED

    @pytest.mark.parametrize(
        "utterance",
        [
            "no",
            "nah",
            "nope",
            "don't",
            "stop",
            "wait",
            "hold on",
            "cancel",
            "never mind",
            "not yet",
            "scratch that",
            "forget it",
        ],
    )
    def test_negative(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.REJECTED

    @pytest.mark.parametrize(
        "utterance",
        [
            "",
            "hmm",
            "what",
            "tell me more",
            "I'm thinking",
        ],
    )
    def test_unclear(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.UNCLEAR

    def test_mixed_defaults_to_unclear(self):
        assert parse_confirmation("yes but wait no") == ConfirmationStatus.UNCLEAR


class TestClassifyAction:
    def test_calling(self):
        assert classify_action("make_phone_call", {}) == ActionCategory.CALLING

    def test_scheduling(self):
        assert classify_action("calendar_create", {}) == ActionCategory.SCHEDULING

    def test_messaging(self):
        assert classify_action("email_send", {}) == ActionCategory.MESSAGING

    def test_other(self):
        assert classify_action("web_search", {}) == ActionCategory.OTHER


class TestConfirmationGate:
    def test_create_gate(self):
        gate = create_gate("calendar_create", {"title": "Meeting"}, "book a meeting")
        assert gate.category == ActionCategory.SCHEDULING
        assert "confirm" in gate.prompt.lower() or "book" in gate.prompt.lower()
        assert gate.status == ConfirmationStatus.PENDING

    def test_build_prompt(self):
        prompt = build_confirmation_prompt(ActionCategory.CALLING, "+14155551234")
        assert "+14155551234" in prompt


class TestRequiresConfirmation:
    def test_read_tools_no_confirm(self):
        assert not requires_confirmation("web_search")
        assert not requires_confirmation("calendar_today")
        assert not requires_confirmation("email_check")

    def test_write_tools_require_confirm(self):
        assert requires_confirmation("calendar_create")
        assert requires_confirmation("email_send")
        assert requires_confirmation("make_phone_call")
