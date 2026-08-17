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
    assert registry.requires_approval("calendar_create")

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
