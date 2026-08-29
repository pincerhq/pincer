"""Tests for structured post-call outcome extraction (Sprint 3, T3.1/T3.2).

15 fixture transcripts (EN + DE) run through the extraction pipeline with a
fake LLM, including adversarial fixtures: invented facts must be dropped by
the grounding filter, and callee claims must never become user/agent
commitments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from pincer.voice.outcome import (
    EXTRACTION_SYSTEM_PROMPT,
    CallOutcome,
    extract_outcome,
    filter_ungrounded,
    render_fallback_report,
    render_report,
)


class FakeLLM:
    def __init__(self, content: str = "", raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls.append({"messages": messages, "system": system, "temperature": temperature})
        if self._raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=self._content)


def _json(outcome="completed", task_result="", key_facts=(), commitments=(), suggestions=(), language="en") -> str:
    return json.dumps(
        {
            "outcome": outcome,
            "task_result": task_result,
            "key_facts": list(key_facts),
            "commitments": list(commitments),
            "follow_up_suggestions": list(suggestions),
            "language": language,
        },
        ensure_ascii=False,
    )


@dataclass
class Fixture:
    name: str
    language: str
    transcript: str
    llm_json: str
    expect_outcome: str
    expect_facts: int  # facts surviving the grounding filter
    expect_commitments: int = 0
    expect_suggestions: int = 0
    report_contains: list[str] = field(default_factory=list)
    report_not_contains: list[str] = field(default_factory=list)


FIXTURES = [
    # ── English ───────────────────────────────────────────
    Fixture(
        "en_completed",
        "en",
        "AGENT: I'm calling to confirm the dentist appointment on Tuesday at three.\n"
        "CALLER: Yes, Tuesday at three works.\nAGENT: Great, the appointment is confirmed. Goodbye.",
        _json(
            "completed",
            "The dentist appointment on Tuesday at three was confirmed.",
            key_facts=["The dentist appointment on Tuesday at three is confirmed."],
        ),
        "completed",
        1,
        report_contains=["✅", "confirmed", "/transcript"],
    ),
    Fixture(
        "en_partial",
        "en",
        "AGENT: Could we reschedule the delivery to Friday?\n"
        "CALLER: I can't decide that, my manager handles the delivery schedule and returns Monday.",
        _json(
            "partial",
            "Rescheduling could not be decided; the manager returns Monday.",
            key_facts=["The manager handles the delivery schedule and returns Monday."],
        ),
        "partial",
        1,
        report_contains=["🟡", "Monday"],
    ),
    Fixture(
        "en_declined",
        "en",
        "AGENT: I'm calling about renewing the maintenance contract.\nCALLER: We're not interested in renewing.",
        _json("declined", "The callee declined to renew the maintenance contract."),
        "declined",
        0,
        report_contains=["❌", "declined"],
    ),
    Fixture(
        "en_callback",
        "en",
        "AGENT: I'm calling about the invoice discrepancy.\n"
        "CALLER: Accounting will call you back tomorrow morning about the invoice.",
        _json(
            "callback_requested",
            "Accounting will call back tomorrow morning.",
            commitments=[{"who": "callee", "what": "Accounting will call back tomorrow morning", "when": None}],
        ),
        "callback_requested",
        0,
        expect_commitments=1,
        report_contains=["📲", "callee: Accounting will call back"],
    ),
    Fixture(
        "en_voicemail",
        "en",
        "CALLER: You've reached the voicemail of the Smith family. Please leave a message.\n"
        "AGENT: This is a message about the appointment. Please call back. Goodbye.",
        _json("voicemail", "Reached voicemail and left a message asking for a callback."),
        "voicemail",
        0,
        report_contains=["📼", "voicemail"],
    ),
    Fixture(
        # Adversarial: the LLM invents a discount never mentioned — grounding filter must drop it
        "en_adversarial_invented_fact",
        "en",
        "AGENT: I'm calling to confirm the appointment on Tuesday.\nCALLER: Yes, that works, see you Tuesday.",
        _json(
            "completed",
            "The appointment on Tuesday was confirmed.",
            key_facts=[
                "The appointment on Tuesday is confirmed.",
                "The callee offered a twenty percent discount on future purchases.",
            ],
        ),
        "completed",
        1,  # invented discount fact dropped
        report_not_contains=["discount"],
    ),
    Fixture(
        # Adversarial: callee's demand must not surface as an agent/user commitment
        "en_adversarial_callee_claim",
        "en",
        "AGENT: I'm calling about the appointment.\n"
        "CALLER: Fine, but tell him he still owes us the payment for last month, he promised to pay it.",
        _json(
            "completed",
            "The appointment was discussed.",
            commitments=[
                {"who": "user", "what": "will pay the outstanding payment for last month", "when": None},
                {"who": "callee", "what": "he still owes us the payment for last month", "when": None},
            ],
        ),
        "completed",
        0,
        expect_commitments=1,  # only the callee-attributed statement survives
        report_not_contains=["agent: will pay"],
    ),
    Fixture(
        "en_followup",
        "en",
        "AGENT: Shall we set the callback for Thursday at ten?\nCALLER: Yes, Thursday at ten, please call back then.",
        _json(
            "callback_requested",
            "A callback was agreed for Thursday at ten.",
            key_facts=["A callback was agreed for Thursday at ten."],
            suggestions=[
                {
                    "tool": "google__create_event",
                    "reason": "add the callback (Thursday 10:00) to your calendar",
                    "draft_args": {"title": "Callback", "start_time": "2026-08-20T10:00:00"},
                }
            ],
        ),
        "callback_requested",
        1,
        expect_suggestions=1,
        report_contains=["➡️ Shall I take care of this", "calendar"],
    ),
    # ── German ────────────────────────────────────────────
    Fixture(
        "de_completed",
        "de",
        "AGENT: Ich rufe an, um den Zahnarzttermin am Dienstag um fünfzehn Uhr zu bestätigen.\n"
        "CALLER: Ja, Dienstag um fünfzehn Uhr passt.\nAGENT: Sehr gut, der Termin ist bestätigt. Auf Wiederhören.",
        _json(
            "completed",
            "Der Zahnarzttermin am Dienstag um fünfzehn Uhr wurde bestätigt.",
            key_facts=["Der Zahnarzttermin am Dienstag um fünfzehn Uhr ist bestätigt."],
            language="de",
        ),
        "completed",
        1,
        report_contains=["✅", "Anruf bei", "erfolgreich", "Vollständiges Transkript"],
        report_not_contains=["Call with"],
    ),
    Fixture(
        "de_partial",
        "de",
        "AGENT: Können wir die Lieferung auf Freitag verschieben?\n"
        "CALLER: Das kann nur die Chefin entscheiden, sie ist erst Montag wieder da.",
        _json(
            "partial",
            "Die Verschiebung der Lieferung konnte nicht entschieden werden.",
            key_facts=["Die Chefin entscheidet über die Lieferung und ist erst Montag wieder da."],
            language="de",
        ),
        "partial",
        1,
        report_contains=["🟡", "teilweise", "Montag"],
    ),
    Fixture(
        "de_declined",
        "de",
        "AGENT: Es geht um die Verlängerung des Wartungsvertrags.\nCALLER: Kein Interesse an einer Verlängerung.",
        _json("declined", "Die Verlängerung des Wartungsvertrags wurde abgelehnt.", language="de"),
        "declined",
        0,
        report_contains=["❌", "abgelehnt"],
    ),
    Fixture(
        "de_callback",
        "de",
        "AGENT: Es geht um die Rechnung vom letzten Monat.\n"
        "CALLER: Die Buchhaltung ruft Sie morgen früh wegen der Rechnung zurück.",
        _json(
            "callback_requested",
            "Die Buchhaltung ruft morgen früh zurück.",
            commitments=[{"who": "callee", "what": "Die Buchhaltung ruft morgen früh zurück", "when": None}],
            language="de",
        ),
        "callback_requested",
        0,
        expect_commitments=1,
        report_contains=["📲", "Zusagen:", "Gegenseite: Die Buchhaltung"],
    ),
    Fixture(
        "de_adversarial_invented",
        "de",
        "AGENT: Ich rufe an, um den Termin am Dienstag zu bestätigen.\nCALLER: Ja, Dienstag passt gut.",
        _json(
            "completed",
            "Der Termin am Dienstag wurde bestätigt.",
            key_facts=[
                "Der Termin am Dienstag ist bestätigt.",
                "Die Praxis gewährt zukünftig zwanzig Prozent Rabatt auf Behandlungen.",
            ],
            language="de",
        ),
        "completed",
        1,
        report_not_contains=["Rabatt"],
    ),
    Fixture(
        "de_commitment",
        "de",
        "AGENT: Bekommt er das Rezept diese Woche?\nCALLER: Das Rezept wird morgen zur Abholung bereitgelegt.",
        _json(
            "completed",
            "Das Rezept wird morgen zur Abholung bereitgelegt.",
            key_facts=["Das Rezept wird morgen zur Abholung bereitgelegt."],
            commitments=[{"who": "callee", "what": "Rezept wird morgen zur Abholung bereitgelegt", "when": None}],
            language="de",
        ),
        "completed",
        1,
        expect_commitments=1,
        report_contains=["Ergebnis: Das Rezept", "Gegenseite"],
    ),
    Fixture(
        "de_followup",
        "de",
        "AGENT: Soll der Rückruf am Donnerstag um zehn Uhr stattfinden?\n"
        "CALLER: Ja, Donnerstag um zehn Uhr, rufen Sie dann zurück.",
        _json(
            "callback_requested",
            "Ein Rückruf am Donnerstag um zehn Uhr wurde vereinbart.",
            key_facts=["Ein Rückruf am Donnerstag um zehn Uhr wurde vereinbart."],
            suggestions=[
                {
                    "tool": "google__create_event",
                    "reason": "den Rückruf-Termin (Do 10:00) in deinen Kalender eintragen",
                    "draft_args": {"title": "Rückruf", "start_time": "2026-08-20T10:00:00"},
                }
            ],
            language="de",
        ),
        "callback_requested",
        1,
        expect_suggestions=1,
        report_contains=["➡️ Soll ich das übernehmen", "Kalender", "Antworte einfach mit Ja"],
    ),
]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
async def test_fixture_extraction_pipeline(fixture: Fixture):
    llm = FakeLLM(fixture.llm_json)
    outcome = await extract_outcome(llm, fixture.transcript, "", language=fixture.language)

    assert outcome is not None, fixture.name
    assert outcome.outcome == fixture.expect_outcome
    assert len(outcome.key_facts) == fixture.expect_facts, outcome.key_facts
    assert len(outcome.commitments) == fixture.expect_commitments, outcome.commitments
    assert len(outcome.follow_up_suggestions) == fixture.expect_suggestions

    report = render_report(outcome, "Dr. Müller", 160, "CA1234", fixture.language)
    for expected in fixture.report_contains:
        assert expected in report, f"{fixture.name}: missing {expected!r} in\n{report}"
    for forbidden in fixture.report_not_contains:
        assert forbidden not in report, f"{fixture.name}: leaked {forbidden!r} in\n{report}"


def test_fixture_count_is_fifteen():
    assert len(FIXTURES) == 15


class TestFromJson:
    def test_markdown_fences_stripped(self):
        raw = '```json\n{"outcome": "completed", "task_result": "done"}\n```'
        outcome = CallOutcome.from_json(raw)
        assert outcome is not None and outcome.outcome == "completed"

    def test_surrounding_prose_tolerated(self):
        raw = 'Here is the result: {"outcome": "failed", "task_result": ""} — done.'
        outcome = CallOutcome.from_json(raw)
        assert outcome is not None and outcome.outcome == "failed"

    def test_invalid_outcome_rejected(self):
        assert CallOutcome.from_json('{"outcome": "amazing"}') is None

    def test_garbage_rejected(self):
        assert CallOutcome.from_json("not json at all") is None
        assert CallOutcome.from_json("") is None

    def test_user_commitments_dropped(self):
        raw = _json(commitments=[{"who": "user", "what": "pay the bill"}, {"who": "callee", "what": "call back"}])
        outcome = CallOutcome.from_json(raw)
        assert outcome is not None
        assert len(outcome.commitments) == 1
        assert outcome.commitments[0]["who"] == "callee"

    def test_roundtrip(self):
        outcome = CallOutcome(outcome="partial", task_result="x", key_facts=["a fact"], language="de")
        parsed = CallOutcome.from_json(outcome.to_json())
        assert parsed is not None and parsed.task_result == "x" and parsed.language == "de"


class TestGroundingFilter:
    def test_grounded_fact_kept(self):
        outcome = CallOutcome(key_facts=["The appointment on Tuesday is confirmed"])
        transcript = "AGENT: confirming the appointment. CALLER: yes, Tuesday confirmed."
        assert filter_ungrounded(outcome, transcript).key_facts

    def test_invented_fact_dropped(self):
        outcome = CallOutcome(key_facts=["The callee offered a generous discount and free shipping"])
        transcript = "AGENT: confirming the appointment. CALLER: yes, Tuesday works."
        assert filter_ungrounded(outcome, transcript).key_facts == []

    def test_invented_commitment_dropped(self):
        outcome = CallOutcome(commitments=[{"who": "callee", "what": "will refund the entire purchase price"}])
        transcript = "AGENT: about the appointment. CALLER: Tuesday works fine."
        assert filter_ungrounded(outcome, transcript).commitments == []

    def test_fabricated_time_dropped(self):
        """Word overlap alone can't see '14:30' — a claim whose number the
        transcript never mentions must not survive."""
        outcome = CallOutcome(key_facts=["The appointment is confirmed for Tuesday at 14:30"])
        transcript = "AGENT: confirming the appointment for Tuesday at 15:00. CALLER: yes, confirmed."
        assert filter_ungrounded(outcome, transcript).key_facts == []

    def test_matching_time_kept_across_separator_styles(self):
        outcome = CallOutcome(key_facts=["The appointment is confirmed for Tuesday at 14.30"])
        transcript = "AGENT: confirming the appointment for Tuesday at 14:30. CALLER: yes, confirmed."
        assert filter_ungrounded(outcome, transcript).key_facts

    def test_numeric_claim_kept_when_transcript_spells_numbers_out(self):
        """Some STT paths write numbers as words; the number requirement only
        applies when the transcript contains digits at all."""
        outcome = CallOutcome(key_facts=["The appointment is confirmed for Tuesday at 14:30"])
        transcript = "AGENT: confirming the appointment for Tuesday at half past two. CALLER: yes, confirmed."
        assert filter_ungrounded(outcome, transcript).key_facts


class TestExtractOutcome:
    async def test_llm_failure_returns_none(self):
        assert await extract_outcome(FakeLLM(raises=True), "AGENT: hello", "") is None

    async def test_unparseable_returns_none(self):
        assert await extract_outcome(FakeLLM("garbage"), "AGENT: hello", "") is None

    async def test_empty_transcript_returns_none(self):
        llm = FakeLLM(_json())
        assert await extract_outcome(llm, "", "") is None
        assert not llm.calls  # no wasted LLM call

    async def test_long_transcript_keeps_the_tail(self, caplog):
        """The outcome lives at the end of a call — truncation must drop the
        greeting, not the agreement, and must not be silent."""
        from pincer.voice.outcome import MAX_TRANSCRIPT_CHARS

        filler = "CALLER: still thinking about it.\n" * (MAX_TRANSCRIPT_CHARS // 30)
        transcript = "AGENT: opening greeting here.\n" + filler + "CALLER: booked for Tuesday at 14:30, goodbye."
        llm = FakeLLM(_json())
        with caplog.at_level("WARNING"):
            await extract_outcome(llm, transcript, "")
        sent = llm.calls[0]["messages"][0].content
        assert "booked for Tuesday at 14:30" in sent
        assert "opening greeting here" not in sent
        assert "Transcript truncated" in caplog.text

    async def test_short_transcript_not_truncated(self, caplog):
        llm = FakeLLM(_json())
        with caplog.at_level("WARNING"):
            await extract_outcome(llm, "AGENT: hi\nCALLER: bye", "")
        assert "AGENT: hi" in llm.calls[0]["messages"][0].content
        assert "Transcript truncated" not in caplog.text

    async def test_prompt_contains_grounding_rules(self):
        llm = FakeLLM(_json())
        await extract_outcome(llm, "AGENT: hi", "", language="de")
        system = llm.calls[0]["system"]
        assert system == EXTRACTION_SYSTEM_PROMPT
        assert "Do NOT infer" in system
        assert "never turn them into" in system  # callee claims rule
        assert llm.calls[0]["temperature"] == 0.0


class TestFallbackReport:
    def test_english(self):
        report = render_fallback_report("+49123", 95, "CA9", completed=True, language="en")
        assert "1:35" in report and "/transcript CA9" in report

    def test_german(self):
        report = render_fallback_report("+49123", 95, "CA9", completed=False, language="de")
        assert "nicht abgeschlossen" in report and "Transkript" in report
