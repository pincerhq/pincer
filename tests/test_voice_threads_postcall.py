"""Sprint 13 §6/§8/§10 — what a threaded call does to its thread after hangup.

Drives the real `PostCallProcessor` so the ordering guarantee that matters is
covered end to end: outcome extraction first, thread merge second, decorated
report last.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import aiosqlite
import pytest

from pincer.voice import status_notify
from pincer.voice import threads as th
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.postcall import PostCallProcessor
from pincer.voice.threads import KIND_FOLLOWUP, KIND_ORIGIN, ThreadManager
from pincer.voice.transcript import Speaker, TranscriptLogger

CALL_SID = "CA_thread_1"

OUTCOME_JSON = json.dumps(
    {
        "outcome": "callback_requested",
        "task_result": "Die Praxis meldet sich mit einem Termin zurück.",
        "key_facts": ["Dr. Müller ist bis Freitag im Urlaub."],
        "commitments": [{"who": "callee", "what": "Die Praxis ruft am Freitag zurück", "when": None}],
        "follow_up_suggestions": [],
        "language": "de",
    },
    ensure_ascii=False,
)

MERGE_JSON = json.dumps(
    {
        "summary": "Dienstag angerufen, Praxis meldet sich.\nStand: wartet auf Rückruf bis Freitag.",
        "satisfied_commitments": [],
    },
    ensure_ascii=False,
)

TRANSCRIPT_LINES = [
    (Speaker.AGENT, "Ich rufe wegen eines Termins bei Dr. Müller an."),
    (Speaker.CALLER, "Dr. Müller ist bis Freitag im Urlaub. Die Praxis ruft am Freitag zurück."),
    (Speaker.AGENT, "Sehr gerne, vielen Dank. Auf Wiederhören."),
]


class SequencedLLM:
    """Returns the extraction response first, the merge response second — the
    order the pipeline is required to call them in."""

    def __init__(self, *payloads: str) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        index = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return SimpleNamespace(content=self._payloads[index])


def _settings(tmp_path):
    return SimpleNamespace(
        db_path=str(tmp_path / "pincer.db"),
        voice_auto_followup=False,
        dashboard_url="https://pincer.test",
        default_user_id="tester",
        thread_inbound_context="off",
        thread_match_window_days=7,
        thread_autoclose_days=30,
    )


def _state() -> CallState:
    return CallState(
        call_sid=CALL_SID,
        direction=CallDirection.OUTBOUND,
        caller_number="+4915212345678",
        target_number="+4930111222",
        target_name="Dr. Müller",
        purpose="Termin bei Dr. Müller",
        language="de",
    )


def _transcript() -> TranscriptLogger:
    transcript = TranscriptLogger(CALL_SID)
    for speaker, text in TRANSCRIPT_LINES:
        transcript.log_utterance(speaker, text)
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
        CALL_SID, user_id="tester", channel="telegram", target_number="+4930111222", language="de"
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


@pytest.fixture
async def threaded(tmp_path, memory):
    """A thread with the call already attached, and the manager installed."""
    settings = _settings(tmp_path)
    manager = ThreadManager(settings.db_path, settings=settings)
    manager.set_memory(memory)
    th.set_thread_manager(manager)
    thread = await manager.create("Termin Dr. Müller", primary_number="+4930111222", language="de")
    await manager.attach(CALL_SID, thread.thread_id, KIND_ORIGIN)
    return SimpleNamespace(settings=settings, manager=manager, thread=thread)


async def test_report_carries_the_thread_header(threaded, notify_capture):
    llm = SequencedLLM(OUTCOME_JSON, MERGE_JSON)
    processor = PostCallProcessor(threaded.settings, llm=llm, memory=None, db_path=threaded.settings.db_path)

    report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    assert notify_capture == [report]
    lines = report.splitlines()
    assert lines[0] == "🧵 Termin Dr. Müller — Anruf 1 von 1"
    # The Sprint 3 report is kept intact underneath the thread header.
    assert "Anruf bei Dr. Müller beendet" in report
    assert "Offen: callee: Die Praxis ruft am Freitag zurück" in report
    assert f"https://pincer.test/voice/threads/{threaded.thread.thread_id}" in report
    assert "✅ Anliegen erledigt." not in report  # a commitment is still open


async def test_summary_and_commitments_land_on_the_thread(threaded):
    processor = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON),
        memory=None,
        db_path=threaded.settings.db_path,
    )
    await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    thread = await threaded.manager.require(threaded.thread.thread_id)
    assert "Stand: wartet auf Rückruf bis Freitag." in thread.rolling_summary
    assert [(c["who"], c["status"]) for c in thread.open_commitments] == [("callee", "open")]
    assert thread.open_commitments[0]["source_call_sid"] == CALL_SID
    assert thread.status == "open"  # callback_requested is not terminal success

    # The member stub carries the outcome, so it survives the transcript purge.
    calls = await threaded.manager.calls(threaded.thread.thread_id)
    assert calls[0].outcome_code == "callback_requested"
    assert calls[0].task_result.startswith("Die Praxis meldet sich")


async def test_call_row_keeps_its_thread_columns_after_persist(threaded):
    """`INSERT OR REPLACE` rewrites the row — the thread link must be restored."""
    processor = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON),
        memory=None,
        db_path=threaded.settings.db_path,
    )
    await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    async with aiosqlite.connect(threaded.settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT thread_id, thread_attach_kind FROM voice_calls WHERE call_sid = ?", (CALL_SID,)
        )
        row = await cursor.fetchone()
    assert row["thread_id"] == threaded.thread.thread_id
    assert row["thread_attach_kind"] == KIND_ORIGIN


async def test_thread_note_replaces_the_previous_one(threaded, memory):
    """§8: one memory note per thread — the chat answer must not go stale, and
    a five-call thread must not leave five contradicting notes behind."""
    processor = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON),
        memory=memory,
        db_path=threaded.settings.db_path,
    )
    await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    tag = f"thread:{threaded.thread.thread_id}"
    notes = await memory.list_memories(user_id=None, limit=20, tags=[tag])
    assert len(notes) == 1
    assert "Termin Dr. Müller" in notes[0].content
    assert "wartet auf Rückruf bis Freitag" in notes[0].content

    # Second call on the same thread: still exactly one note, now updated.
    second_sid = "CA_thread_2"
    await threaded.manager.attach(second_sid, threaded.thread.thread_id, KIND_FOLLOWUP)
    status_notify.register_outbound_call(second_sid, user_id="tester", channel="telegram", language="de")
    updated_merge = json.dumps(
        {"summary": "Praxis hat zurückgerufen.\nStand: erledigt.", "satisfied_commitments": []},
        ensure_ascii=False,
    )
    state = _state()
    state.call_sid = second_sid
    transcript = TranscriptLogger(second_sid)
    transcript.log_utterance(Speaker.CALLER, "Die Praxis ruft am Freitag zurück, das ist erledigt.")
    processor2 = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, updated_merge),
        memory=memory,
        db_path=threaded.settings.db_path,
    )
    await processor2.process(second_sid, state, transcript, completed=True)

    notes = await memory.list_memories(user_id=None, limit=20, tags=[tag])
    assert len(notes) == 1
    assert "erledigt" in notes[0].content


async def test_threadless_call_report_is_unchanged(tmp_path, notify_capture, memory):
    """Every pre-Sprint-13 call still gets exactly the Sprint 3 report."""
    settings = _settings(tmp_path)
    th.set_thread_manager(ThreadManager(settings.db_path, settings=settings))
    processor = PostCallProcessor(
        settings, llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON), memory=memory, db_path=settings.db_path
    )

    report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    assert "🧵" not in report
    assert report.startswith("📲") or "Anruf bei Dr. Müller beendet" in report


async def test_thread_update_failure_still_delivers_the_report(threaded, notify_capture, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("thread store down")

    monkeypatch.setattr(ThreadManager, "update_after_call", _boom)
    processor = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON),
        memory=None,
        db_path=threaded.settings.db_path,
    )

    report = await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    assert notify_capture == [report]
    assert "Anruf bei Dr. Müller beendet" in report
    assert "🧵" not in report


# ── §12 integration: the three-call thread ───────────────────────────


async def test_three_call_thread_carries_tuesday_into_call_three(threaded, notify_capture, memory):
    """attempt → voicemail retry → success.

    The point of the whole sprint: by call three the agent's live system prompt
    contains what happened on call one, so it can say "wie am Dienstag
    besprochen" instead of starting from zero.
    """
    from pincer.voice.state_machine import CallStateMachine

    manager = threaded.manager
    manager.set_memory(memory)
    tid = threaded.thread.thread_id

    attempts = [
        (
            CALL_SID,
            json.dumps(
                {
                    "outcome": "callback_requested",
                    "task_result": "Praxis meldet sich wegen des Termins zurück.",
                    "key_facts": ["Dr. Müller ist bis Freitag im Urlaub."],
                    "commitments": [{"who": "callee", "what": "Die Praxis ruft am Freitag zurück", "when": None}],
                    "follow_up_suggestions": [],
                    "language": "de",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "summary": "Am Dienstag angerufen, Dr. Müller ist im Urlaub.\nStand: Praxis ruft Freitag zurück.",
                    "satisfied_commitments": [],
                },
                ensure_ascii=False,
            ),
        ),
        (
            "CA_thread_2",
            json.dumps(
                {
                    "outcome": "voicemail",
                    "task_result": "Nur der Anrufbeantworter erreicht.",
                    "key_facts": [],
                    "commitments": [],
                    "follow_up_suggestions": [],
                    "language": "de",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "summary": (
                        "Am Dienstag angerufen, Dr. Müller ist im Urlaub. "
                        "Zweiter Versuch am Mittwoch nur auf dem Anrufbeantworter.\n"
                        "Stand: Praxis ruft Freitag zurück."
                    ),
                    "satisfied_commitments": [],
                },
                ensure_ascii=False,
            ),
        ),
    ]

    for index, (sid, outcome_json, merge_json) in enumerate(attempts):
        if index:
            await manager.attach(sid, tid, KIND_FOLLOWUP)
            status_notify.register_outbound_call(sid, user_id="tester", channel="telegram", language="de")
        state = _state()
        state.call_sid = sid
        transcript = TranscriptLogger(sid)
        transcript.log_utterance(
            Speaker.CALLER, "Dr. Müller ist bis Freitag im Urlaub. Die Praxis ruft am Freitag zurück."
        )
        processor = PostCallProcessor(
            threaded.settings,
            llm=SequencedLLM(outcome_json, merge_json),
            memory=memory,
            db_path=threaded.settings.db_path,
        )
        report = await processor.process(sid, state, transcript, completed=True)
        assert f"— Anruf {index + 1} von {index + 1}" in report

    # Call three is about to be placed on the same matter.
    await manager.attach("CA_thread_3", tid, KIND_FOLLOWUP)
    state = _state()
    state.call_sid = "CA_thread_3"

    from pincer.channels.phone_calls import VoiceChannel

    channel = VoiceChannel(threaded.settings)
    await channel._prepare_thread_context("CA_thread_3", state)
    sm = CallStateMachine("CA_thread_3", is_outbound=True)
    sm.start_call()
    prompt = channel._build_voice_system(state, sm)

    assert "THREAD-KONTEXT" in prompt
    assert "Am Dienstag angerufen" in prompt
    assert "Anrufbeantworter" in prompt
    assert "Die Praxis ruft am Freitag zurück" in prompt  # the open commitment rides along
    assert len(state.metadata["thread_context"]) <= th.MAX_CONTEXT_BLOCK


async def test_chat_can_answer_status_from_memory(threaded, memory):
    """§8: "Was ist der Stand bei Dr. Müller?" is an ordinary memory search."""
    processor = PostCallProcessor(
        threaded.settings,
        llm=SequencedLLM(OUTCOME_JSON, MERGE_JSON),
        memory=memory,
        db_path=threaded.settings.db_path,
    )
    await processor.process(CALL_SID, _state(), _transcript(), completed=True)

    hits = await memory.search_text("Müller", user_id=None, limit=5)
    assert any("wartet auf Rückruf bis Freitag" in m.content for m in hits)


async def test_ambiguous_followup_surfaces_both_threads(threaded, tmp_path):
    """§4.2: two open threads for one contact must both come back, so the agent
    asks which one instead of guessing."""
    await threaded.manager.create("Rechnung Dr. Müller", contact_name="Dr. Müller", primary_number="+4930111222")

    lookup = th.make_thread_lookup(threaded.settings)
    found = json.loads(await lookup("Dr. Müller"))
    assert len(found) == 2
    assert {t["subject"] for t in found} == {"Termin Dr. Müller", "Rechnung Dr. Müller"}
