"""Tests for the post-call pipeline (Sprint 3): persist → extract → memory →
report → follow-up proposals, plus the /transcript retrieval tool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import pytest
from support import SEED_ID_SQL

from pincer.voice import status_notify
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.postcall import PostCallProcessor
from pincer.voice.transcript import Speaker, TranscriptLogger

CALL_SID = "CA_post_1"


class FakeLLM:
    def __init__(self, content: str = "", raises: bool = False) -> None:
        self._content = content
        self._raises = raises

    async def complete(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=None,
        temperature=None,
        system=None,
        reasoning_effort=None,
        thinking=None,
        pinned=False,
    ):
        if self._raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=self._content)


GOOD_JSON = json.dumps(
    {
        "outcome": "completed",
        "task_result": "Der Zahnarzttermin am Dienstag wurde bestätigt.",
        "key_facts": ["Dr. Müller ist bis zum zwanzigsten im Urlaub."],
        "commitments": [{"who": "callee", "what": "Die Praxis ruft bei Rückfragen zurück", "when": None}],
        "follow_up_suggestions": [
            {
                "tool": "google__create_event",
                "reason": "den Rückruf-Termin in deinen Kalender eintragen",
                "draft_args": {"title": "Rückruf Praxis"},
            }
        ],
        "language": "de",
    },
    ensure_ascii=False,
)

TRANSCRIPT_LINES = [
    (Speaker.AGENT, "Ich rufe an, um den Zahnarzttermin am Dienstag zu bestätigen."),
    (Speaker.CALLER, "Dr. Müller ist bis zum zwanzigsten im Urlaub, aber der Termin am Dienstag ist bestätigt."),
    (Speaker.CALLER, "Die Praxis ruft bei Rückfragen zurück."),
    (Speaker.AGENT, "Sehr gut, der Termin ist bestätigt. Auf Wiederhören."),
]


def _settings(tmp_path, auto_followup=False):
    return SimpleNamespace(db_path=tmp_path / "pincer.db", voice_auto_followup=auto_followup)


def _state() -> CallState:
    state = CallState(
        call_sid=CALL_SID,
        direction=CallDirection.OUTBOUND,
        caller_number="+491761234567",
        target_number="+491761234567",
        target_name="Dr. Müller",
        purpose="Zahnarzttermin bestätigen",
        language="de",
    )
    # Answered, as the engine stamps it: an outbound call without it never
    # connected and gets the not-connected report.
    state.metadata["answered_at"] = datetime.now(UTC)
    return state


def _transcript() -> TranscriptLogger:
    transcript = TranscriptLogger(CALL_SID)
    for speaker, text in TRANSCRIPT_LINES:
        transcript.log_utterance(speaker, text)
    transcript.log_action("tool_call", "calendar_confirm", output_summary="Appointment confirmed")
    return transcript


@pytest.fixture
def notify_capture():
    status_notify._reset_for_tests()
    sent: list[str] = []

    async def notifier(user_id, channel, text):
        sent.append(text)
        return True

    status_notify.set_status_notifier(notifier)
    status_notify.register_outbound_call(
        CALL_SID, user_id="tester", channel="telegram", target_number="+491761234567", language="de"
    )
    yield sent
    status_notify._reset_for_tests()


@pytest.fixture
async def memory(tmp_path):
    from pincer.memory.sqlite import SQLiteMemoryBackend

    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    await backend.initialize()
    yield backend
    await backend.close()


class TestPostCallProcessor:
    async def test_full_pipeline(self, tmp_path, notify_capture, memory):
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=FakeLLM(GOOD_JSON), memory=memory, db_path=str(settings.db_path))

        report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)

        # Report is German, structured, sent exactly once
        assert notify_capture == [report]
        assert "✅ Anruf bei Dr. Müller beendet" in report
        assert "Ergebnis: Der Zahnarzttermin" in report
        assert "Urlaub" in report
        assert "Zusagen: Gegenseite: Die Praxis ruft" in report
        assert "➡️ Soll ich das übernehmen" in report
        assert f"/transcript {CALL_SID}" in report

        # Persistence: call row, transcript rows, and the outcome audit action
        async with aiosqlite.connect(str(settings.db_path)) as db:
            calls = await db.execute_fetchall("SELECT call_sid, direction FROM pincer_voice_calls")
            assert calls == [(CALL_SID, "outbound")]
            entries = await db.execute_fetchall(
                "SELECT COUNT(*) FROM pincer_call_transcripts WHERE call_id = ?", (CALL_SID,)
            )
            assert entries[0][0] == len(TRANSCRIPT_LINES)
            outcome_rows = await db.execute_fetchall(
                "SELECT output_summary FROM pincer_call_actions WHERE call_id = ? AND action_type = 'outcome'",
                (CALL_SID,),
            )
            assert len(outcome_rows) == 1
            assert json.loads(outcome_rows[0][0])["outcome"] == "completed"

        # Memory: key fact findable via normal search (T3.3 acceptance)
        hits = await memory.search_text("Urlaub", user_id="tester")
        assert hits and "Dr. Müller" in hits[0].content
        # Commitment + follow-up draft also remembered
        all_notes = await memory.list_memories(user_id="tester", category="voice_call", limit=20)
        contents = [m.content for m in all_notes]
        assert any("Commitment (callee)" in c for c in contents)
        assert any("Proposed follow-up" in c and "google__create_event" in c for c in contents)

    async def test_extraction_failure_falls_back_to_basic_report(self, tmp_path, notify_capture, memory):
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=FakeLLM(raises=True), memory=memory, db_path=str(settings.db_path))

        report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)

        assert notify_capture == [report]
        assert "abgeschlossen" in report  # German fallback
        assert f"/transcript {CALL_SID}" in report
        # No memory notes without an outcome
        assert await memory.count(user_id="tester") == 0
        # Transcript still persisted
        async with aiosqlite.connect(str(settings.db_path)) as db:
            entries = await db.execute_fetchall("SELECT COUNT(*) FROM pincer_call_transcripts")
            assert entries[0][0] == len(TRANSCRIPT_LINES)

    async def test_no_transcript_still_reports(self, tmp_path, notify_capture):
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=FakeLLM(GOOD_JSON), db_path=str(settings.db_path))

        report = await processor.process(CALL_SID, _state(), None, completed=False)
        assert notify_capture == [report]
        assert "nicht abgeschlossen" in report

    async def test_no_llm_uses_fallback(self, tmp_path, notify_capture):
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=None, db_path=str(settings.db_path))
        report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)
        assert "abgeschlossen" in report

    async def test_unverified_claim_caveat_appended(self, tmp_path, notify_capture):
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=FakeLLM(GOOD_JSON), db_path=str(settings.db_path))
        report = await processor.process(
            CALL_SID, _state(), _transcript(), completed=True, unverified_claims=["ist gebucht"]
        )
        assert "⚠️ Hinweis" in report

    async def test_english_call_gets_english_report(self, tmp_path):
        status_notify._reset_for_tests()
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call(CALL_SID, user_id="tester", channel="telegram", language="en")

        english_json = json.dumps(
            {
                "outcome": "completed",
                "task_result": "The appointment was confirmed.",
                "key_facts": [],
                "commitments": [],
                "follow_up_suggestions": [],
                "language": "en",
            }
        )
        settings = _settings(tmp_path)
        processor = PostCallProcessor(settings, llm=FakeLLM(english_json), db_path=str(settings.db_path))
        state = _state()
        state.language = "en"
        transcript = TranscriptLogger(CALL_SID)
        transcript.log_utterance(Speaker.AGENT, "Calling to confirm the appointment.")
        transcript.log_utterance(Speaker.CALLER, "Yes, the appointment is confirmed.")

        report = await processor.process(CALL_SID, state, transcript, completed=True)
        assert "Call with" in report and "Anruf" not in report
        status_notify._reset_for_tests()


class TestTranscriptTool:
    async def _seed(self, db_path):
        from pincer.voice.retention import ensure_voice_tables

        async with aiosqlite.connect(str(db_path)) as db:
            await ensure_voice_tables(db)
            await db.execute(
                "INSERT INTO pincer_voice_calls (id, call_sid, direction, to_number, pincer_user_id, started_at) "
                f"VALUES ({SEED_ID_SQL}, ?, ?, ?, ?, ?)",
                ("CA_t1", "outbound", "+491761234567", "tester", "2026-08-16T10:00:00+00:00"),
            )
            await db.execute(
                "INSERT INTO pincer_call_transcripts (id, call_id, speaker, text, timestamp) "
                f"VALUES ({SEED_ID_SQL}, ?, ?, ?, ?)",
                ("CA_t1", "agent", "My card number is 4111 1111 1111 1111 okay?", "2026-08-16T10:00:01+00:00"),
            )
            await db.execute(
                "INSERT INTO pincer_call_transcripts (id, call_id, speaker, text, timestamp) "
                f"VALUES ({SEED_ID_SQL}, ?, ?, ?, ?)",
                ("CA_t1", "caller", "Der Termin ist bestätigt.", "2026-08-16T10:00:02+00:00"),
            )
            await db.commit()

    async def test_returns_masked_transcript(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript("CA_t1", context={"pincer_user_id": "tester"})
        assert "4111 1111 1111 1111" not in result  # PII masked
        assert "Der Termin ist bestätigt." in result
        assert "AGENT:" in result and "CALLER:" in result

    async def test_empty_sid_returns_latest(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript(context={"pincer_user_id": "tester"})
        assert "CA_t1" in result

    async def test_missing_call(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        assert "No transcript found" in await get_call_transcript("CA_nope", context={"pincer_user_id": "tester"})

    async def test_no_db_yet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=tmp_path / "missing.db"),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript(context={"pincer_user_id": "tester"})
        assert "No calls found" in result or "No call transcripts" in result

    async def test_other_users_sid_is_not_found(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript("CA_t1", context={"pincer_user_id": "intruder"})
        assert "No transcript found" in result
        assert "Termin" not in result

    async def test_latest_call_scoped_to_user(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript(context={"pincer_user_id": "intruder"})
        assert "No calls found" in result

    async def test_no_identity_is_rejected(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pincer.db"
        await self._seed(db_path)
        monkeypatch.setattr(
            "pincer.tools.builtin.call_transcript.get_settings",
            lambda: SimpleNamespace(db_path=db_path),
        )
        from pincer.tools.builtin.call_transcript import get_call_transcript

        result = await get_call_transcript("CA_t1")
        assert "no user identity" in result
        assert "Termin" not in result


class _Notes:
    def __init__(self) -> None:
        self.contents: list[str] = []

    async def store_memory(self, user_id, content, **_kwargs):
        self.contents.append(content)
        return str(len(self.contents))


def _remembered_args(note: str) -> dict:
    return json.loads(note.split("args=", 1)[1])


async def test_both_kinds_of_follow_up_are_remembered_in_the_calls_zone(tmp_path):
    """PR #316 review 3 (S4): outcome follow-ups were stored with offset-less times, so a later "yes, do
    it" in chat read them in the calendar's zone. Deferred and outcome follow-ups are pinned alike."""
    from pincer.voice.outcome import CallOutcome

    settings = SimpleNamespace(db_path=tmp_path / "pincer.db", voice_timezone="Europe/Berlin", timezone="Europe/Kyiv")
    notes = _Notes()
    processor = PostCallProcessor(settings, memory=notes)
    draft = {"summary": "Dentist", "start": "2026-11-03T14:00:00", "end": "2026-11-03T15:00:00"}
    pinned = {
        **draft,
        "start": "2026-11-03T14:00:00+01:00",
        "end": "2026-11-03T15:00:00+01:00",
        "timezone": "Europe/Berlin",
    }

    await processor._write_memory_notes(
        CALL_SID,
        "tester",
        "Dr. X",
        CallOutcome(follow_up_suggestions=[{"tool": "google__create_event", "draft_args": draft}]),
    )
    await processor._write_deferred_followups(
        CALL_SID, "tester", "Dr. X", [{"tool": "google__create_event", "draft_args": draft, "reason": "tool_timeout"}]
    )

    remembered = [_remembered_args(c) for c in notes.contents if "Proposed follow-up" in c]
    assert remembered == [pinned, pinned]


async def test_a_completed_call_report_reaches_its_chat_session_and_is_stored(tmp_path):
    """Issue #303: the agent's own history gets the report, not only the channel."""
    from pincer.services.voice import CallsService

    status_notify._reset_for_tests()
    settings = _settings(tmp_path)
    status_notify.set_report_store(settings.db_path)
    sent: list[str] = []
    notes: list[tuple[str, str, str, str]] = []

    async def notifier(user_id, channel, text):
        sent.append(text)
        return True

    async def reporter(pincer_user_id, channel, session_key, text):
        notes.append((pincer_user_id, channel, session_key, text))

    status_notify.set_status_notifier(notifier)
    status_notify.set_session_reporter(reporter)
    await status_notify.track_outbound_call(
        CALL_SID,
        user_id="12345",
        channel="telegram",
        target_name="Dr. Müller",
        language="de",
        pincer_user_id="alice",
        session_key="unified:alice",
    )
    try:
        processor = PostCallProcessor(settings, llm=FakeLLM(GOOD_JSON), db_path=str(settings.db_path))
        report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)
        await status_notify.drain()

        assert sent == [report]
        assert notes == [("alice", "telegram", "unified:alice", f"📞 Call report (Dr. Müller):\n{report}")]
        row = await (await CallsService.for_path(settings.db_path)).get_call_origin(CALL_SID)
        assert row is not None
        assert (row["report_outcome"], row["report_text"]) == ("completed", report)
    finally:
        status_notify._reset_for_tests()


async def test_an_answered_call_that_failed_is_stored_as_failed(tmp_path):
    """An answered call that did not complete is not reported as `completed`."""
    from pincer.services.voice import CallsService

    status_notify._reset_for_tests()
    settings = _settings(tmp_path)
    status_notify.set_report_store(settings.db_path)
    status_notify.register_outbound_call(CALL_SID, user_id="12345", channel="telegram", language="de")
    try:
        processor = PostCallProcessor(settings, llm=None, db_path=str(settings.db_path))
        state = _state()
        state.metadata["failure_code"] = "llm_error"
        await processor.process(CALL_SID, state, _transcript(), completed=False)

        row = await (await CallsService.for_path(settings.db_path)).get_call_origin(CALL_SID)
        assert row is not None
        assert row["report_outcome"] == "failed"
    finally:
        status_notify._reset_for_tests()


# ── the daily budget (issue #357, PR #366 review) ─────────────────────────────


async def _priced_tracker(tmp_path, budget: float):  # type: ignore[no-untyped-def]
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(tmp_path / "pincer.db", daily_budget=budget, prices=load_price_table())
    await tracker.initialize()
    return tracker


def _outcome_llm():  # type: ignore[no-untyped-def]
    """The outcome extraction answers GOOD_JSON, priced: 1,000 input tokens of test-fixture-model, $0.007."""
    from unittest.mock import AsyncMock

    from pincer.llm.base import BaseLLMProvider, LLMResponse

    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.return_value = LLMResponse(
        content=GOOD_JSON, provider="anthropic", request_model="test-fixture-model", input_tokens=1_000
    )
    return llm


async def _cost_rows(tracker) -> list[tuple[str, float]]:  # type: ignore[no-untyped-def]
    summary = await tracker.get_costs_by_model("2000-01-01", "2100-01-01")
    return [(row["model"], row["total"]) for row in summary]


async def test_a_finished_call_s_outcome_extraction_is_recorded(tmp_path, notify_capture):  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    tracker = await _priced_tracker(tmp_path, budget=0.0)
    llm = _outcome_llm()
    processor = PostCallProcessor(settings, llm=llm, db_path=str(settings.db_path), cost_tracker=tracker)
    try:
        report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)
        rows = await _cost_rows(tracker)
    finally:
        await tracker.close()

    assert "Ergebnis: Der Zahnarzttermin" in report  # precondition: the extraction ran
    assert llm.complete.await_count == 1
    assert rows == [("test-fixture-model", pytest.approx(0.007))]


async def test_with_the_budget_reached_a_finished_call_gets_the_basic_report_without_a_request(  # type: ignore[no-untyped-def]
    tmp_path, notify_capture, caplog
):
    import logging

    from pincer.config.pricing import TokenUsage

    settings = _settings(tmp_path)
    tracker = await _priced_tracker(tmp_path, budget=0.007)
    await tracker.record("anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000))  # today = the budget
    llm = _outcome_llm()
    processor = PostCallProcessor(settings, llm=llm, db_path=str(settings.db_path), cost_tracker=tracker)
    try:
        with caplog.at_level(logging.INFO):
            report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)
        rows = await _cost_rows(tracker)
    finally:
        await tracker.close()

    assert llm.complete.await_count == 0
    assert "abgeschlossen" in report  # the basic (fallback) report
    assert rows == [("test-fixture-model", pytest.approx(0.007))]  # only the seeded spend
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("pincer.voice")] == []


@pytest.mark.parametrize("budget_reached", [False, True])
async def test_a_thread_summary_is_metered_and_falls_back_when_the_budget_is_reached(  # type: ignore[no-untyped-def]
    tmp_path, caplog, budget_reached: bool
):
    import logging
    from unittest.mock import AsyncMock

    from pincer.config.pricing import TokenUsage
    from pincer.llm.base import BaseLLMProvider, LLMResponse
    from pincer.llm.cost_tracker import MeteredLLM
    from pincer.voice.threads import merge_summary

    tracker = await _priced_tracker(tmp_path, budget=0.007)
    if budget_reached:
        await tracker.record("anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000))
    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.return_value = LLMResponse(
        content='{"summary": "Merged.", "satisfied_commitments": []}',
        provider="anthropic",
        request_model="test-fixture-model",
        input_tokens=1_000,
    )
    try:
        with caplog.at_level(logging.INFO, logger="pincer.voice.threads"):
            summary, _, failed = await merge_summary(
                MeteredLLM(llm, tracker, "anthropic"), "Earlier.", {"task_result": "completed"}, [], "en"
            )
        spent = await tracker.get_today_spend()
    finally:
        await tracker.close()

    assert llm.complete.await_count == (0 if budget_reached else 1)
    assert (summary == "Merged.", failed) == (not budget_reached, budget_reached)
    assert spent == pytest.approx(0.007)  # the seed, or the one merge request
    levels = [r.levelname for r in caplog.records if r.name == "pincer.voice.threads"]
    assert levels == (["INFO"] if budget_reached else [])
