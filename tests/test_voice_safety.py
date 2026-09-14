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


class TestNegatedAffirmatives:
    """A qualified rejection must never read as a confirmation.

    The inline lookarounds only excluded a negation directly against the
    affirmative word, and most affirmatives ("sure", "correct", "genau",
    "korrekt") carried no guard at all — so "that's not quite right" and even
    "I'm not sure" returned CONFIRMED and the gated action went ahead.
    """

    @pytest.mark.parametrize(
        "utterance",
        [
            "That's not quite right",
            "not quite right",
            "that is not correct",
            "not really correct",
            "I'm not sure",
            "I am not at all sure",
            "I don't think that's right",
            "that doesn't sound right",
        ],
    )
    def test_english_qualified_rejection(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.REJECTED

    @pytest.mark.parametrize(
        "utterance",
        [
            "nicht ganz richtig",
            "Das ist nicht ganz richtig",
            "nicht so ganz richtig",
            "nicht wirklich korrekt",
            "nicht ganz genau",
            "das ist nicht in ordnung",
        ],
    )
    def test_german_qualified_rejection(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.REJECTED

    @pytest.mark.parametrize(
        "utterance",
        [
            "yes",
            "ja",
            "sure",
            "correct",
            "korrekt",
            "genau",
            "that is right",
            "yes, that's right",
            "ja, das ist richtig",
            "go ahead",
            "in ordnung",
            "einverstanden",
        ],
    )
    def test_plain_affirmatives_still_confirm(self, utterance):
        """The negation scope must not swallow genuine confirmations."""
        assert parse_confirmation(utterance) == ConfirmationStatus.CONFIRMED

    def test_negation_does_not_cross_a_clause_boundary(self):
        # The "no" belongs to its own clause; the later "yes" is untouched by
        # it, so this stays the mixed signal it has always been.
        assert parse_confirmation("No problem, yes go ahead") == ConfirmationStatus.UNCLEAR

    def test_negated_and_genuine_affirmative_is_unclear(self):
        assert parse_confirmation("not quite right, but yes go ahead") == ConfirmationStatus.UNCLEAR

    def test_german_postposed_negation_still_works(self):
        # No look-back can see these; the forward lookaheads still carry them.
        assert parse_confirmation("stimmt nicht") == ConfirmationStatus.REJECTED
        assert parse_confirmation("das passt mir nicht") == ConfirmationStatus.REJECTED
        assert parse_confirmation("das passt nicht ganz") == ConfirmationStatus.REJECTED

    def test_distant_negation_does_not_reach(self):
        """Negation is windowed, so an early 'no' cannot flip a far-off yes."""
        assert parse_confirmation("no") == ConfirmationStatus.REJECTED
        # Same clause but well beyond the window: the affirmative survives and
        # the explicit negative still forces a re-ask rather than an execution.
        assert parse_confirmation("no I said I would be happy to proceed") == ConfirmationStatus.UNCLEAR


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
        assert not requires_confirmation("file_read")
        assert not requires_confirmation("calendar_today")
        assert not requires_confirmation("email_check")

    def test_write_tools_require_confirm(self):
        assert requires_confirmation("calendar_create")
        assert requires_confirmation("email_send")
        assert requires_confirmation("make_phone_call")
