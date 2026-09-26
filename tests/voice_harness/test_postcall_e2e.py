"""End-to-end post-call flow through the harness (Sprint 3, T3.5):
call → report → memory note → follow-up proposal → approval → tool executes.

The follow-up executes through the standard tool registry + approval gate —
no parallel mechanism; denial leaves no side effects.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pincer.tools.registry import ToolRegistry
from pincer.voice.postcall import PostCallProcessor

from .personas_de import CooperativePersonaDe
from .runner import Scenario, run_scenario


class FakeLLM:
    def __init__(self, content: str, raises: bool = False) -> None:
        self._content = content
        self._raises = raises

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        if self._raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=self._content)


# Grounded in the German scripted dialogue (Termin, Dienstag, fünfzehn Uhr, bestätigt)
OUTCOME_JSON = json.dumps(
    {
        "outcome": "completed",
        "task_result": "Der Zahnarzttermin am Dienstag um fünfzehn Uhr wurde bestätigt.",
        "key_facts": ["Der Zahnarzttermin am Dienstag um fünfzehn Uhr ist bestätigt."],
        "commitments": [],
        "follow_up_suggestions": [
            {
                "tool": "calendar_create",
                "reason": "den Termin (Dienstag 15:00) in deinen Kalender eintragen",
                "draft_args": {"title": "Zahnarzt", "start_time": "2026-08-18T15:00:00"},
            }
        ],
        "language": "de",
    },
    ensure_ascii=False,
)


@pytest.fixture
async def memory(tmp_path):
    from pincer.memory.sqlite import SQLiteMemoryBackend

    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    await backend.initialize()
    yield backend
    await backend.close()


async def test_call_report_memory_followup_approval(tmp_path, memory):
    settings = SimpleNamespace(db_path=tmp_path / "pincer.db", voice_auto_followup=False)
    processor = PostCallProcessor(
        settings,
        llm=FakeLLM(OUTCOME_JSON),
        memory=memory,
        db_path=str(settings.db_path),
    )

    scenario = Scenario("postcall_e2e", CooperativePersonaDe, expects_task_done=True, language="de")
    result = await run_scenario(scenario, post_call_processor=processor)

    # 1. Call succeeded and the final message is the structured German report
    assert result.ok
    report = result.status_messages[-1]
    assert "✅ Anruf bei" in report
    assert "Ergebnis: Der Zahnarzttermin" in report
    assert "➡️ Soll ich das übernehmen" in report  # follow-up proposal reached the user
    assert len(result.status_messages) <= 3

    # 2. Key fact landed in cross-channel memory, findable via normal search
    hits = await memory.search_text("Zahnarzttermin", user_id="tester")
    assert hits, "call fact not findable in memory"
    followups = await memory.list_memories(user_id="tester", tags=["followup"], limit=5)
    assert followups and "calendar_create" in followups[0].content

    # 3. User approves → the follow-up executes through the STANDARD registry +
    #    approval gate (mocked calendar tool)
    executed: list[dict] = []

    async def calendar_create(title: str, start_time: str) -> str:
        executed.append({"title": title, "start_time": start_time})
        return f"Event created: '{title}'"

    registry = ToolRegistry()
    registry.register(
        name="calendar_create",
        description="Create a calendar event",
        handler=calendar_create,
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}, "start_time": {"type": "string"}},
            "required": ["title", "start_time"],
        },
        require_approval=True,
    )

    draft_args = {"title": "Zahnarzt", "start_time": "2026-08-18T15:00:00"}
    assert registry.declares_approval("calendar_create")

    async def approve(tool_name, args, user_id, channel):
        return True

    # This mirrors the agent loop: approval gate first, then execute
    approved = await approve("calendar_create", draft_args, "tester", "telegram")
    assert approved
    output = await registry.execute("calendar_create", dict(draft_args))
    assert "Event created" in output
    assert executed == [draft_args]


async def test_denied_followup_leaves_no_side_effects():
    executed: list[dict] = []

    async def calendar_create(title: str, start_time: str) -> str:
        executed.append({"title": title})
        return "Event created"

    registry = ToolRegistry()
    registry.register(
        name="calendar_create",
        description="Create a calendar event",
        handler=calendar_create,
        parameters={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        require_approval=True,
    )

    async def deny(tool_name, args, user_id, channel):
        return False

    approved = await deny("calendar_create", {"title": "Zahnarzt"}, "tester", "telegram")
    assert not approved
    # Agent loop skips execution on denial — nothing ran
    assert executed == []


async def test_extraction_failure_still_reports_through_channel(tmp_path):
    """Regression (T3.5): a broken extraction LLM must still yield the basic
    summary report via the channel path."""
    settings = SimpleNamespace(db_path=tmp_path / "pincer.db", voice_auto_followup=False)
    processor = PostCallProcessor(settings, llm=FakeLLM("", raises=True), db_path=str(settings.db_path))

    scenario = Scenario("postcall_fallback", CooperativePersonaDe, expects_task_done=True, language="de")
    result = await run_scenario(scenario, post_call_processor=processor)

    assert result.ok
    report = result.status_messages[-1]
    assert "abgeschlossen" in report  # German fallback report
    assert "/transcript" in report


class TestOptOutSources:
    """Auto-blacklisting reads the CALLEE's words only.

    The scan used to take every entry with `speaker != Speaker.AGENT`, which
    includes the SYSTEM `[BRIEFING]` line written at call start. A user whose
    briefing said "ask them to stop calling me" had the number they asked us to
    dial silently added to the shared do-not-call list — on the strength of
    their own instruction, without the callee ever asking for anything.
    """

    BRIEFING = "call the gym and ask them to stop calling me about the membership"

    @staticmethod
    def _transcript(call_sid: str, briefing: str, callee_lines: list[str]):
        from pincer.voice.transcript import Speaker, TranscriptLogger

        transcript = TranscriptLogger(call_sid)
        transcript.log_utterance(Speaker.SYSTEM, f"[BRIEFING] {briefing}", state="briefing")
        transcript.log_utterance(Speaker.AGENT, "Hello, calling on behalf of Mr Schmidt.")
        for line in callee_lines:
            transcript.log_utterance(Speaker.CALLER, line)
        return transcript

    @staticmethod
    def _state(call_sid: str):
        from pincer.voice.engine import CallDirection, CallState

        return CallState(
            call_sid=call_sid,
            direction=CallDirection.OUTBOUND,
            caller_number="+4930111222",
            target_number="+4930999888",
        )

    async def _run(self, monkeypatch, callee_lines: list[str]) -> list[str]:
        """Returns the numbers added to the do-not-call list."""
        added: list[str] = []

        async def _fake_add(settings, number, reason="", source="", call_sid=""):
            added.append(number)

        monkeypatch.setattr("pincer.voice.safety_gates.add_do_not_call", _fake_add)

        processor = PostCallProcessor(SimpleNamespace(db_path=":memory:"))
        await processor._honor_opt_out(
            "CA_opt",
            self._state("CA_opt"),
            self._transcript("CA_opt", self.BRIEFING, callee_lines),
            None,
            "en",
        )
        return added

    async def test_briefing_text_does_not_blacklist_the_callee(self, monkeypatch):
        added = await self._run(monkeypatch, ["Sure, I have cancelled the membership."])
        assert added == [], "the user's own briefing must never opt the callee out"

    async def test_the_callee_actually_asking_still_blacklists(self, monkeypatch):
        """The feature itself must keep working."""
        added = await self._run(monkeypatch, ["Please never call me again."])
        assert added == ["+4930999888"]

    async def test_the_agent_quoting_the_request_does_not_blacklist(self, monkeypatch):
        """The agent echoes the request back when apologising."""
        from pincer.voice.transcript import Speaker, TranscriptLogger

        added: list[str] = []

        async def _fake_add(settings, number, reason="", source="", call_sid=""):
            added.append(number)

        monkeypatch.setattr("pincer.voice.safety_gates.add_do_not_call", _fake_add)

        transcript = TranscriptLogger("CA_echo")
        transcript.log_utterance(Speaker.AGENT, "Of course — I will never call you again. Sorry to disturb.")
        transcript.log_utterance(Speaker.CALLER, "Thanks, goodbye.")

        processor = PostCallProcessor(SimpleNamespace(db_path=":memory:"))
        await processor._honor_opt_out("CA_echo", self._state("CA_echo"), transcript, None, "en")

        assert added == []


class TestSharedPipelineCoversEveryCallType:
    """Receptionist calls used to `return` before the shared post-call steps.

    `save_analytics`/`record_metrics`, thread folding and do-not-call opt-out
    honouring all lived in the outbound body only, so an inbound caller asking
    never to be called again never reached the shared list an outbound callee
    would, and no `call_analytics` row was ever written for a receptionist call.
    """

    @staticmethod
    def _state(*, receptionist: bool):
        from pincer.voice.engine import CallDirection, CallState

        state = CallState(
            call_sid="CA_shared",
            direction=CallDirection.INBOUND if receptionist else CallDirection.OUTBOUND,
            caller_number="+4930999888",
            target_number="" if receptionist else "+4930999888",
        )
        if receptionist:
            state.metadata["receptionist"] = True
            state.metadata["reception"] = {"intent": "message", "caller_name": "Schmidt"}
        return state

    @staticmethod
    def _transcript(callee_line: str):
        from pincer.voice.transcript import Speaker, TranscriptLogger

        transcript = TranscriptLogger("CA_shared")
        transcript.log_utterance(Speaker.AGENT, "Praxis Dr. Müller, guten Tag.")
        transcript.log_utterance(Speaker.CALLER, callee_line)
        return transcript

    async def _run(self, monkeypatch, *, receptionist: bool, callee_line: str) -> dict:
        seen: dict = {"opt_out": [], "analytics": [], "metrics": 0, "thread": 0, "report": ""}

        async def _fake_add(settings, number, reason="", source="", call_sid=""):
            seen["opt_out"].append(number)

        async def _fake_save(db_path, call_sid, analytics):
            seen["analytics"].append(call_sid)

        def _fake_metrics(analytics, **kwargs):
            seen["metrics"] += 1

        async def _fake_thread(self, call_sid, outcome, language):
            seen["thread"] += 1
            return None

        monkeypatch.setattr("pincer.voice.safety_gates.add_do_not_call", _fake_add)
        monkeypatch.setattr("pincer.voice.analytics.save_analytics", _fake_save)
        monkeypatch.setattr("pincer.voice.analytics.record_metrics", _fake_metrics)

        async def _deliver(settings, call_sid, report):
            seen["report"] = report
            return True

        monkeypatch.setattr(PostCallProcessor, "_update_thread", _fake_thread)
        monkeypatch.setattr("pincer.voice.receptionist.report.deliver_owner_report", _deliver)

        processor = PostCallProcessor(SimpleNamespace(db_path=":memory:", voice_default_language="de"))
        await processor.process(
            "CA_shared", self._state(receptionist=receptionist), self._transcript(callee_line), True
        )
        return seen

    async def test_receptionist_call_writes_analytics(self, monkeypatch):
        seen = await self._run(monkeypatch, receptionist=True, callee_line="Alles gut, danke.")
        assert seen["analytics"] == ["CA_shared"]
        assert seen["metrics"] == 1

    async def test_receptionist_call_folds_into_its_thread(self, monkeypatch):
        seen = await self._run(monkeypatch, receptionist=True, callee_line="Alles gut, danke.")
        assert seen["thread"] == 1

    async def test_inbound_caller_opting_out_reaches_the_shared_list(self, monkeypatch):
        seen = await self._run(monkeypatch, receptionist=True, callee_line="Rufen Sie mich bitte nie wieder an.")
        assert seen["opt_out"] == ["+4930999888"], "an inbound opt-out must bind like an outbound one"

    async def test_the_owner_report_says_the_caller_opted_out(self, monkeypatch):
        seen = await self._run(monkeypatch, receptionist=True, callee_line="Rufen Sie mich bitte nie wieder an.")
        assert "Sperrliste" in seen["report"], "the owner must learn the caller asked not to be contacted"

    async def test_a_quiet_inbound_caller_is_not_blacklisted(self, monkeypatch):
        seen = await self._run(monkeypatch, receptionist=True, callee_line="Alles gut, danke.")
        assert seen["opt_out"] == []

    async def test_outbound_still_runs_the_same_steps(self, monkeypatch):
        """The hoist must not have dropped anything from the path that had them."""
        seen = await self._run(monkeypatch, receptionist=False, callee_line="Rufen Sie mich bitte nie wieder an.")
        assert seen["analytics"] == ["CA_shared"]
        assert seen["metrics"] == 1
        assert seen["thread"] == 1
        assert seen["opt_out"] == ["+4930999888"]


class TestPersistFailureIsVisible:
    """A failed end-of-call write must reach the user, not just the log.

    `_persist` swallowed every exception and returned None, so a locked
    database, a full disk or schema drift produced a normal-looking report for
    a call that `/transcript <sid>` can never find and whose briefing_json and
    thread bookkeeping were never stored.
    """

    @staticmethod
    def _state(*, receptionist: bool = False):
        from pincer.voice.engine import CallDirection, CallState

        state = CallState(
            call_sid="CA_persist",
            direction=CallDirection.INBOUND if receptionist else CallDirection.OUTBOUND,
            caller_number="+4930111222",
            target_number="" if receptionist else "+4930999888",
        )
        if receptionist:
            state.metadata["receptionist"] = True
            state.metadata["reception"] = {"intent": "message", "caller_name": "Schmidt"}
        return state

    @staticmethod
    def _transcript():
        from pincer.voice.transcript import Speaker, TranscriptLogger

        transcript = TranscriptLogger("CA_persist")
        transcript.log_utterance(Speaker.AGENT, "Guten Tag.")
        transcript.log_utterance(Speaker.CALLER, "Alles klar, danke.")
        return transcript

    async def _report(self, monkeypatch, tmp_path, *, receptionist: bool, fail: bool) -> str:
        delivered: dict = {"report": ""}

        async def _deliver(settings, call_sid, report):
            delivered["report"] = report
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr("pincer.voice.receptionist.report.deliver_owner_report", _deliver)
        monkeypatch.setattr("pincer.voice.analytics.save_analytics", _noop)
        monkeypatch.setattr(PostCallProcessor, "_update_thread", _noop)
        if fail:

            async def _boom(*args, **kwargs):
                raise RuntimeError("database is locked")

            monkeypatch.setattr("pincer.services.voice.CallsService.save_call", _boom)

        processor = PostCallProcessor(SimpleNamespace(db_path=str(tmp_path / "voice.db"), voice_default_language="de"))
        report = await processor.process("CA_persist", self._state(receptionist=receptionist), self._transcript(), True)
        return delivered["report"] if receptionist else report

    async def test_outbound_report_says_the_call_was_not_saved(self, monkeypatch, tmp_path):
        report = await self._report(monkeypatch, tmp_path, receptionist=False, fail=True)
        assert "could not be fully saved" in report or "nicht vollständig gespeichert" in report

    async def test_receptionist_report_says_the_call_was_not_saved(self, monkeypatch, tmp_path):
        report = await self._report(monkeypatch, tmp_path, receptionist=True, fail=True)
        assert "nicht vollständig gespeichert" in report

    async def test_a_healthy_write_adds_no_warning(self, monkeypatch, tmp_path):
        report = await self._report(monkeypatch, tmp_path, receptionist=False, fail=False)
        assert "could not be fully saved" not in report
        assert "nicht vollständig gespeichert" not in report

    async def test_persist_returns_false_only_on_a_real_failure(self, monkeypatch, tmp_path):
        processor = PostCallProcessor(SimpleNamespace(db_path=str(tmp_path / "voice.db")))
        assert await processor._persist("CA_ok", self._state(), self._transcript()) is True

        async def _boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr("pincer.services.voice.CallsService.save_call", _boom)
        assert await processor._persist("CA_bad", self._state(), self._transcript()) is False

    async def test_no_store_configured_is_not_reported_as_a_failure(self):
        """Warning on every call in that setup would train the user to ignore
        the line that matters."""
        processor = PostCallProcessor(SimpleNamespace(db_path=""))
        assert await processor._persist("CA_nodb", self._state(), self._transcript()) is True

    async def test_the_report_still_reaches_the_user_when_the_write_fails(self, monkeypatch, tmp_path):
        """Losing the record must not also cost the user their report."""
        report = await self._report(monkeypatch, tmp_path, receptionist=False, fail=True)
        assert report.strip(), "a persistence failure must not suppress the report"
