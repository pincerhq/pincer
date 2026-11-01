"""
One calendar toolset for the model (calendar reliability, slice D) — agent-level E2E.

Each test drives ``Agent.handle_message`` with a scripted LLM (no real model)
over the registry that ``register_default_tools`` really builds, with both
calendar families wired to one stateful fake Calendar service. What is
asserted is the calendar's state afterwards: how many events exist, their
titles, IDs and stored start instants.

The owner's production screenshot showed "Meeting" plus a fresh
"Meeting (updated)" in the same slot: the model created because it had no
event ID in hand. The ID-less paths reproduced here:

- the builtin family (``calendar_create`` / ``calendar_week``) never prints an
  event ID and has no update, so "move it" can only become a second create;
- an edit arriving in another channel's session (``whatsapp:`` vs
  ``telegram:`` — sessions are per channel) has no ID in context;
- an edit after the session trim dropped the create result.

What the scripted model can and cannot prove: its policy is written down in
``ScriptedLLM``. When no event ID is in context it searches first only if the
*offered* tool descriptions steer edits to the update tool (the create tool
forbids rename/reschedule, or the update tool names them); otherwise it
creates a copy, as the production model did. So the cross-session and trim
tests prove that the steering text reaches the model in every turn's tool
schemas and that search → update then works end to end — not that a real
model obeys the text (the live smoke run covers that).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.config import Settings
from pincer.core.agent import Agent
from pincer.core.session import SessionManager
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.llm.cost_tracker import CostTracker
from pincer.tools.bootstrap import register_default_tools
from pincer.tools.registry import ToolRegistry

KYIV = ZoneInfo("Europe/Kyiv")
BUILTIN_CALENDAR_TOOLS = {"calendar_today", "calendar_week", "calendar_create"}
OWNER = "owner"

_CREATE = re.compile(r'^create "(?P<title>[^"]+)" on (?P<day>\d{4}-\d{2}-\d{2}) at (?P<at>\d{2}:\d{2})$')
_MOVE = re.compile(r'^move "(?P<title>[^"]+)" to (?P<at>\d{2}:\d{2})$')
_RENAME = re.compile(r'^rename "(?P<title>[^"]+)" to "(?P<new>[^"]+)"$')
_ID_LINE = re.compile(r"ID:\s*(\S+)")


def _steers_edits_to_update(tools: dict[str, str]) -> bool:
    """Whether the offered descriptions send rename/reschedule to the update tool."""
    create = tools.get("google__create_event", "").lower()
    update = tools.get("google__update_event", "").lower()
    return ("never" in create and "rename" in create) or "rename" in update


def _event_id_in(results: list[str], title: str) -> str | None:
    """The ID printed for ``title`` in any tool result (renderer: title, then ``ID:``)."""
    for text in results:
        at = text.find(title)
        while at != -1:
            match = _ID_LINE.search(text, at)
            if match:
                return match.group(1)
            at = text.find(title, at + 1)
    return None


class ScriptedLLM(BaseLLMProvider):
    """A rule-based stand-in for the model, reading only what a model would see.

    Inputs per call: the latest user message, the tool results in the session
    (all of them for event IDs, this turn's to know what it already did), and
    the tool schemas offered this turn.

    - ``create "T" on D at HH:MM`` → create (builtin ``calendar_create`` when
      ``prefer_builtins`` and it is offered, else ``google__create_event``).
    - ``move "T" to HH:MM`` / ``rename "T" to "N"``:
      an ID for T in context → ``google__update_event``;
      no ID and nothing read yet this turn → read: ``calendar_week`` when
      ``prefer_builtins`` and offered; else ``google__search_events`` if the
      offered descriptions steer edits to the update tool; else no read —
      create a copy (the production behaviour);
      after a read that showed no ID → create a copy.
    - a tool result already answering the request → final text.
    - anything else → small talk.
    """

    def __init__(self, *, prefer_builtins: bool = False) -> None:
        self.prefer_builtins = prefer_builtins
        self.offered: list[set[str]] = []
        self.called: list[str] = []
        self._ids = itertools.count(1)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        offered = {t["name"]: str(t.get("description", "")) for t in tools or []}
        self.offered.append(set(offered))
        last_user = max(i for i, m in enumerate(messages) if m.role == MessageRole.USER)
        text = messages[last_user].content
        all_results = [m.content for m in messages if m.role == MessageRole.TOOL_RESULT]
        turn_calls = [tc.name for m in messages[last_user:] for tc in m.tool_calls]
        if turn_calls and turn_calls[-1] in {"google__update_event", "google__create_event", "calendar_create"}:
            return self._say("Done.")

        if m := _CREATE.match(text):
            return self._create(offered, m["title"], m["day"], m["at"])
        edit = _MOVE.match(text) or _RENAME.match(text)
        if edit is None:
            return self._say("Sure.")
        title = edit["title"]
        changes = {"summary": edit["new"]} if "new" in edit.groupdict() else {"at": edit["at"]}
        event_id = _event_id_in(all_results, title)
        if event_id is not None:
            return self._update(event_id, changes)
        read_done = bool({"calendar_week", "google__search_events"} & set(turn_calls))
        if not read_done:
            if self.prefer_builtins and "calendar_week" in offered:
                return self._call("calendar_week", {})
            if "google__search_events" in offered and _steers_edits_to_update(offered):
                return self._call("google__search_events", {"query": title})
        return self._copy(offered, title, changes)

    def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        async def _tokens() -> AsyncIterator[str]:
            response = await self.complete(messages, tools=tools, system=system)
            if response.content:
                yield response.content

        return _tokens()

    async def close(self) -> None:
        return None

    # ── responses ────────────────────────────────────────────────────────
    def _say(self, text: str) -> LLMResponse:
        return LLMResponse(content=text, model="scripted", input_tokens=1, output_tokens=1, stop_reason="end_turn")

    def _call(self, name: str, arguments: dict[str, Any]) -> LLMResponse:
        self.called.append(name)
        call = ToolCall(id=f"call_{next(self._ids)}", name=name, arguments=arguments)
        return LLMResponse(tool_calls=[call], model="scripted", input_tokens=1, output_tokens=1, stop_reason="tool_use")

    def _create(self, offered: dict[str, str], title: str, day: str, at: str) -> LLMResponse:
        if self.prefer_builtins and "calendar_create" in offered:
            return self._call("calendar_create", {"title": title, "start_time": f"{day}T{at}:00"})
        start = datetime.fromisoformat(f"{day}T{at}:00")
        end = start + timedelta(hours=1)
        return self._call(
            "google__create_event", {"summary": title, "start": start.isoformat(), "end": end.isoformat()}
        )

    def _update(self, event_id: str, changes: dict[str, str]) -> LLMResponse:
        if "summary" in changes:
            return self._call("google__update_event", {"event_id": event_id, "summary": changes["summary"]})
        start = datetime.fromisoformat(f"{_tomorrow()}T{changes['at']}:00")
        return self._call(
            "google__update_event",
            {"event_id": event_id, "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()},
        )

    def _copy(self, offered: dict[str, str], title: str, changes: dict[str, str]) -> LLMResponse:
        """No ID in hand: create the changed event anew (what produced the duplicate)."""
        new_title = changes.get("summary", title)
        at = changes.get("at", "10:00")
        return self._create(offered, new_title, _tomorrow(), at)


def _tomorrow() -> str:
    return (datetime.now(KYIV) + timedelta(days=1)).date().isoformat()


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake() -> FakeCalendarService:
    return FakeCalendarService({"primary": "Europe/Kyiv"})


@pytest.fixture
def berlin_settings(tmp_path, monkeypatch) -> Settings:
    """The owner's deployment: PINCER_TIMEZONE=Europe/Berlin, calendar in Kyiv."""
    settings = Settings(
        anthropic_api_key="sk-ant-test-key",  # type: ignore[arg-type]
        data_dir=tmp_path / ".pincer",
        daily_budget_usd=100.0,
        timezone="Europe/Berlin",
    )
    relaxed = SimpleNamespace(timezone="Europe/Berlin", voice_timezone="Europe/Berlin")
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: relaxed)
    monkeypatch.setattr("pincer.tools.builtin.calendar_tool.get_settings", lambda: settings)
    return settings


@pytest.fixture
def registry(fake, berlin_settings, monkeypatch) -> ToolRegistry:
    """The production bootstrap, with every calendar tool pointed at ``fake``."""
    factory = FakeCalendarFactory(fake)
    factory.auth = SimpleNamespace(services=["calendar"])  # type: ignore[attr-defined]
    monkeypatch.setattr("pincer.integrations.google.get_google_factory", lambda: factory)
    monkeypatch.setattr("pincer.tools.builtin.calendar_tool._get_service", lambda: fake)
    reg = ToolRegistry()
    register_default_tools(reg, berlin_settings)
    assert "google__update_event" in reg.list_tools()  # Workspace tools really registered
    return reg


@pytest_asyncio.fixture
async def costs(tmp_path) -> AsyncIterator[CostTracker]:
    tracker = CostTracker(tmp_path / "costs.db", daily_budget=100.0)
    await tracker.initialize()
    yield tracker
    await tracker.close()


async def _sessions(tmp_path, max_messages: int) -> SessionManager:
    manager = SessionManager(tmp_path / f"sessions-{max_messages}.db", max_messages=max_messages)
    await manager.initialize()
    return manager


async def _approve(*_args: Any) -> bool:
    return True


def _agent(settings: Settings, llm: ScriptedLLM, sessions: SessionManager, costs: CostTracker, reg: ToolRegistry):
    return Agent(settings, llm, sessions, costs, reg, approval_callback=_approve)


async def _say(agent: Agent, channel: str, text: str) -> None:
    await agent.handle_message(OWNER, channel, text, memory=False)


def _kyiv(day: str, at: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{at}:00").replace(tzinfo=KYIV)


# ── tests ────────────────────────────────────────────────────────────────────


def test_builtin_calendar_tools_are_not_registered(registry: ToolRegistry) -> None:
    assert BUILTIN_CALENDAR_TOOLS & set(registry.list_tools()) == set()


def test_cancelling_an_event_still_needs_approval(registry: ToolRegistry) -> None:
    assert registry.requires_approval("google__delete_event") is True


async def test_create_then_move_updates_the_one_event(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """A model that reaches for the builtins whenever they are offered.

    Pre-slice: calendar_create (no ID) → calendar_week (no IDs) → calendar_create
    again — two events. After: create and update the same Workspace event.
    """
    llm = ScriptedLLM(prefer_builtins=True)
    sessions = await _sessions(tmp_path, 50)
    try:
        agent = _agent(berlin_settings, llm, sessions, costs, registry)
        day = _tomorrow()
        await _say(agent, "whatsapp", f'create "Standup" on {day} at 10:00')
        await _say(agent, "whatsapp", 'move "Standup" to 17:00')
    finally:
        await sessions.close()

    events = fake.events_on("primary")
    assert len(events) == 1, [(e["summary"], e["start"]) for e in events]
    assert fake.start_of("primary", events[0]["id"]) == _kyiv(day, "17:00")
    assert not any(BUILTIN_CALENDAR_TOOLS & names for names in llm.offered)


async def test_rename_from_another_channel_renames_the_event(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """The screenshot: created in WhatsApp, renamed from Telegram (ID not in context)."""
    llm = ScriptedLLM()
    sessions = await _sessions(tmp_path, 50)
    try:
        agent = _agent(berlin_settings, llm, sessions, costs, registry)
        day = _tomorrow()
        await _say(agent, "whatsapp", f'create "Meeting" on {day} at 10:00')
        (original,) = fake.events_on("primary")
        telegram = await sessions.get_or_create(OWNER, "telegram")
        assert not any(original["id"] in m.content for m in telegram.messages)  # no ID in this session
        await _say(agent, "telegram", 'rename "Meeting" to "Meeting (updated)"')
    finally:
        await sessions.close()

    events = fake.events_on("primary")
    assert sorted(e["summary"] for e in events) == ["Meeting (updated)"]
    assert events[0]["id"] == original["id"]
    assert fake.start_of("primary", original["id"]) == _kyiv(day, "10:00")


async def test_move_after_history_trim_searches_then_updates(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """The create result has been trimmed out of the session before "move it"."""
    llm = ScriptedLLM()
    sessions = await _sessions(tmp_path, 8)
    try:
        agent = _agent(berlin_settings, llm, sessions, costs, registry)
        day = _tomorrow()
        await _say(agent, "whatsapp", f'create "Dentist" on {day} at 10:00')
        (original,) = fake.events_on("primary")
        session = await sessions.get_or_create(OWNER, "whatsapp")
        for n in range(10):
            if not any(original["id"] in m.content for m in session.messages):
                break
            await _say(agent, "whatsapp", f"thanks {n}")
        assert not any(original["id"] in m.content for m in session.messages)  # trimmed away
        await _say(agent, "whatsapp", 'move "Dentist" to 17:00')
    finally:
        await sessions.close()

    events = fake.events_on("primary")
    assert len(events) == 1, [(e["summary"], e["start"]) for e in events]
    assert events[0]["id"] == original["id"]
    assert fake.start_of("primary", original["id"]) == _kyiv(day, "17:00")
