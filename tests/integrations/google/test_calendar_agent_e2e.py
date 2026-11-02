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
from calendar_support import BUILTIN_CALENDAR_TOOLS

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.config import Settings
from pincer.core.agent import Agent
from pincer.core.session import SessionManager
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.llm.cost_tracker import CostTracker
from pincer.tools.bootstrap import register_default_tools
from pincer.tools.registry import ToolRegistry

KYIV = ZoneInfo("Europe/Kyiv")
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

    - ``create "T" on D at HH:MM`` → ``google__create_event``.
    - ``move "T" to HH:MM`` / ``rename "T" to "N"``:
      an ID for T in context → ``google__update_event``;
      no ID and nothing read yet this turn → read: ``google__search_events`` if
      the offered descriptions steer edits to the update tool; else no read —
      create a copy (the production behaviour);
      after a read that showed no ID → create a copy.
    - a tool result already answering the request → final text.
    - anything else → small talk.
    """

    def __init__(self) -> None:
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
        if turn_calls and turn_calls[-1] in {"google__update_event", "google__create_event"}:
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
        read_done = "google__search_events" in turn_calls
        if not read_done and "google__search_events" in offered and _steers_edits_to_update(offered):
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
    return settings


@pytest.fixture
def registry(fake, berlin_settings, monkeypatch) -> ToolRegistry:
    """The production bootstrap, with every calendar tool pointed at ``fake``."""
    factory = FakeCalendarFactory(fake)
    factory.auth = SimpleNamespace(services=["calendar"])  # type: ignore[attr-defined]
    monkeypatch.setattr("pincer.integrations.google.get_google_factory", lambda: factory)
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
    """Pre-slice the model reached for the offered builtins: calendar_create (no ID) →
    calendar_week (no IDs) → calendar_create again — two events. Now no builtin is
    offered, and create then move updates the same Workspace event."""
    llm = ScriptedLLM()
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


# ── calls: the voice zone (B1) ───────────────────────────────────────────────


class _OneCallLLM(BaseLLMProvider):
    """Emits one tool call (offset-less times, no ``timezone``, as the voice prompt asks), then "Done."."""

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self._call = ToolCall(id="call_voice_1", name=name, arguments=arguments)
        self.offered: list[set[str]] = []
        self.results: list[str] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.offered.append({t["name"] for t in tools or []})
        last_user = max(i for i, m in enumerate(messages) if m.role == MessageRole.USER)
        if results := [m.content for m in messages[last_user:] if m.role == MessageRole.TOOL_RESULT]:
            self.results = results
            return LLMResponse(
                content="Done.", model="scripted", input_tokens=1, output_tokens=1, stop_reason="end_turn"
            )
        return LLMResponse(
            tool_calls=[self._call], model="scripted", input_tokens=1, output_tokens=1, stop_reason="tool_use"
        )

    def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):  # type: ignore[no-untyped-def]
        async def _tokens() -> AsyncIterator[str]:
            response = await self.complete(messages, tools=tools, system=system)
            if response.content:
                yield response.content

        return _tokens()

    async def close(self) -> None:
        return None


def _call_gate(settings: Settings, kind: str, registry: ToolRegistry):
    """The gate of a call with a registered caller: writes follow the standard approval policy, as
    ``PhoneCallManager`` wires it (a tool the registry does not know is confirmed out loud)."""
    from pincer.voice.engine import CallDirection, CallState
    from pincer.voice.in_call_tools import InCallToolGate
    from pincer.voice.state_machine import CallStateMachine
    from pincer.voice.tool_policy import CALLER_USER, META_CALLER_CLASS, allowed_tools_for_call

    state = CallState(call_sid="CA-B1", direction=CallDirection.OUTBOUND, caller_number="+4930123")
    state.metadata[META_CALLER_CLASS] = CALLER_USER
    return InCallToolGate(
        call_sid="CA-B1",
        state=state,
        sm=CallStateMachine("CA-B1", is_outbound=True),
        settings=settings,
        allowed_tools=allowed_tools_for_call(settings, kind=kind, caller_class=CALLER_USER),
        requires_approval=lambda name: name not in registry.list_tools() or registry.requires_approval(name),
    )


async def _on_a_call(tmp_path, settings: Settings, llm: _OneCallLLM, costs, registry, kind: str) -> None:
    from pincer.voice.in_call_tools import bind_gate

    sessions = await _sessions(tmp_path, 50)
    try:
        agent = Agent(settings, llm, sessions, costs, registry, approval_callback=_approve)
        with bind_gate(_call_gate(settings, kind, registry)):
            await agent.handle_message(OWNER, "voice", "book it", memory=False)
    finally:
        await sessions.close()


@pytest.mark.parametrize("voice_zone", ["Europe/Berlin", "Europe/London"])
async def test_a_booking_on_a_call_is_stored_in_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs, voice_zone
) -> None:
    """The call prompt says "pass calendar times as local wall-clock time in <voice zone>": a caller's
    14:00 must be 14:00 there, not 14:00 in the (Kyiv) calendar's zone."""
    berlin_settings.voice_timezone = voice_zone
    day = _tomorrow()
    llm = _OneCallLLM(
        "google__create_event", {"summary": "Dentist", "start": f"{day}T14:00:00", "end": f"{day}T15:00:00"}
    )

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "appointment")

    assert "google__create_event" in llm.offered[0]  # precondition: the call offered the tool
    (event,) = fake.events_on("primary")
    assert fake.start_of("primary", event["id"]) == datetime.fromisoformat(f"{day}T14:00:00").replace(
        tzinfo=ZoneInfo(voice_zone)
    )


async def test_a_zone_the_model_names_on_a_call_is_kept(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """The voice zone fills in a missing timezone only: an explicit one is never overridden."""
    day = _tomorrow()
    llm = _OneCallLLM(
        "google__create_event",
        {"summary": "Dentist", "start": f"{day}T14:00:00", "end": f"{day}T15:00:00", "timezone": "Europe/Kyiv"},
    )

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "appointment")

    (event,) = fake.events_on("primary")
    assert fake.start_of("primary", event["id"]) == _kyiv(day, "14:00")


async def test_a_move_on_a_call_reads_the_new_time_in_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs
) -> None:
    """On a call, update reads an offset-less time in the voice zone (the zone the call shows times in),
    and the event keeps its own stored zone (Kyiv here)."""
    day = _tomorrow()
    event_id = fake.seed_event(
        "primary", "Dentist", _kyiv(day, "10:00").isoformat(), _kyiv(day, "11:00").isoformat(), time_zone="Europe/Kyiv"
    )
    llm = _OneCallLLM("google__update_event", {"event_id": event_id, "start": f"{day}T14:00:00"})

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert "google__update_event" in llm.offered[0]  # precondition: the call offered the tool
    assert fake.start_of("primary", event_id) == datetime.fromisoformat(f"{day}T14:00:00").replace(
        tzinfo=ZoneInfo("Europe/Berlin")
    )
    assert fake.event("primary", event_id)["start"]["timeZone"] == "Europe/Kyiv"  # the event's own zone is kept


def _read_bounds(fake: FakeCalendarService, tool: str) -> tuple[str, str]:
    """The ``timeMin``/``timeMax`` the read tool really sent to Google."""
    if tool == "google__list_events":
        (sent,) = fake.sent("events.list")
        return sent["timeMin"], sent["timeMax"]
    (sent,) = fake.sent("freebusy.query")
    return sent["body"]["timeMin"], sent["body"]["timeMax"]


@pytest.mark.parametrize(
    ("tool", "extra"), [("google__list_events", {}), ("google__check_freebusy", {"emails": "primary"})]
)
async def test_an_availability_read_on_a_call_queries_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs, tool, extra
) -> None:
    """The call prompt gives times in the voice zone, so "is 14:00-15:00 free?" must check 14:00 Berlin,
    not 14:00 in the (Kyiv) calendar's zone — else the check and the booking disagree by an hour."""
    day = _tomorrow()
    llm = _OneCallLLM(tool, {"time_min": f"{day}T14:00:00", "time_max": f"{day}T15:00:00", **extra})

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert tool in llm.offered[0]  # precondition: the call offered the tool
    berlin = ZoneInfo("Europe/Berlin")
    assert _read_bounds(fake, tool) == (
        datetime.fromisoformat(f"{day}T14:00:00").replace(tzinfo=berlin).isoformat(),
        datetime.fromisoformat(f"{day}T15:00:00").replace(tzinfo=berlin).isoformat(),
    )


async def test_a_move_on_a_call_keeps_the_events_own_zone(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """A New York event moved on a call to "17:00" (start only) lands at 17:00 in the voice zone (Berlin),
    keeps its 90 minutes and stays a New York event: the call's zone says when, not where the event lives."""
    day = _tomorrow()
    new_york = ZoneInfo("America/New_York")
    event_id = fake.seed_event(
        "primary",
        "Board call",
        datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=new_york).isoformat(),
        datetime.fromisoformat(f"{day}T11:30:00").replace(tzinfo=new_york).isoformat(),
        time_zone="America/New_York",
    )
    llm = _OneCallLLM("google__update_event", {"event_id": event_id, "start": f"{day}T17:00:00"})

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert "google__update_event" in llm.offered[0]  # precondition: the call offered the tool
    stored = fake.event("primary", event_id)
    assert (stored["start"].get("timeZone"), stored["end"].get("timeZone")) == ("America/New_York", "America/New_York")
    starts = datetime.fromisoformat(f"{day}T17:00:00").replace(tzinfo=ZoneInfo("Europe/Berlin"))
    assert fake.instants(stored, "Europe/Kyiv") == (starts, starts + timedelta(minutes=90))


async def test_a_search_on_a_call_without_a_gate_reads_its_bounds_in_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs
) -> None:
    """PR #316 review: "is my dentist appointment between 14:00 and 16:00?" on a call (the no-gate
    voice path, which offers search) must search 14:00-16:00 Berlin, not Kyiv: an event at 15:30
    Berlin (16:30 Kyiv) is otherwise missed."""
    day = _tomorrow()
    berlin = ZoneInfo("Europe/Berlin")
    at = datetime.fromisoformat(f"{day}T15:30:00").replace(tzinfo=berlin)
    fake.seed_event("primary", "Dentist", at.isoformat(), (at + timedelta(minutes=30)).isoformat())
    llm = _OneCallLLM(
        "google__search_events", {"query": "Dentist", "time_min": f"{day}T14:00:00", "time_max": f"{day}T16:00:00"}
    )
    sessions = await _sessions(tmp_path, 50)
    try:
        agent = Agent(berlin_settings, llm, sessions, costs, registry, approval_callback=_approve)
        async for _chunk in agent.stream_voice_turn(OWNER, "voice", "is my dentist between two and four?"):
            pass
    finally:
        await sessions.close()

    assert "google__search_events" in llm.offered[0]  # precondition: the no-gate call path offered it
    (sent,) = fake.sent("events.list")
    assert (sent["timeMin"], sent["timeMax"]) == (
        datetime.fromisoformat(f"{day}T14:00:00").replace(tzinfo=berlin).isoformat(),
        datetime.fromisoformat(f"{day}T16:00:00").replace(tzinfo=berlin).isoformat(),
    )
    assert "Found 1 event(s)" in llm.results[0], llm.results


async def test_a_list_on_a_call_is_spoken_in_the_voice_zone(tmp_path, fake, berlin_settings, registry, costs) -> None:
    """PR #316 review: on a Kyiv calendar with a Berlin voice zone, an event at 10:00 Berlin must be
    listed, and so spoken, as 10 AM — not 11 AM (its Kyiv wall clock). The gate hands the model the
    spoken rendering (voice/tool_speech.py) of the list result."""
    day = _tomorrow()
    at = datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=ZoneInfo("Europe/Berlin"))
    fake.seed_event("primary", "Dentist", at.isoformat(), (at + timedelta(hours=1)).isoformat())
    llm = _OneCallLLM("google__list_events", {"time_min": day, "time_max": day})

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert "google__list_events" in llm.offered[0]  # precondition: the call offered the tool
    assert "There are 1 appointment(s)" in llm.results[0], llm.results  # precondition: spoken list text
    assert "at 10:00 AM: Dentist" in llm.results[0], llm.results


def _spoken_day(day: str) -> str:
    from pincer.voice.tool_speech import spoken_datetime

    return spoken_datetime(datetime.fromisoformat(day), "en", include_time=False)


async def test_a_free_busy_check_on_a_call_is_spoken_in_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs
) -> None:
    """PR #316 review: the call's offset-less window made the speech renderer compare naive and aware
    datetimes, and the caller heard the tool-error line for a check that worked. The window and the
    busy slots are in the voice zone: busy 15:00-16:00 Berlin leaves 14-15 and 16-18 free."""
    day = _tomorrow()
    at = datetime.fromisoformat(f"{day}T15:00:00").replace(tzinfo=ZoneInfo("Europe/Berlin"))
    fake.seed_event("primary", "Dentist", at.isoformat(), (at + timedelta(hours=1)).isoformat())
    llm = _OneCallLLM(
        "google__check_freebusy", {"emails": "primary", "time_min": f"{day}T14:00:00", "time_max": f"{day}T18:00:00"}
    )

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert "google__check_freebusy" in llm.offered[0]  # precondition: the call offered the tool
    assert fake.sent("freebusy.query")  # precondition: the check really ran
    spoken = _spoken_day(day)
    assert llm.results == [
        f"[TOOL RESULT: Free would be: {spoken} from 2:00 PM to 3:00 PM and {spoken} from 4:00 PM to 6:00 PM.]"
    ]


async def test_busy_times_without_a_window_on_a_call_are_spoken_in_the_voice_zone(
    tmp_path, fake, berlin_settings, registry, costs
) -> None:
    """PR #316 review: with no bounds the renderer speaks only the busy side, from Google's UTC slots;
    10:00-11:00 Berlin must be spoken as 10 AM, not 9 AM (its UTC wall clock in winter)."""
    day = _tomorrow()
    at = datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=ZoneInfo("Europe/Berlin"))
    fake.seed_event("primary", "Dentist", at.isoformat(), (at + timedelta(hours=1)).isoformat())
    llm = _OneCallLLM("google__check_freebusy", {"emails": "primary"})

    await _on_a_call(tmp_path, berlin_settings, llm, costs, registry, "generic")

    assert fake.sent("freebusy.query")  # precondition: the check really ran
    assert llm.results == [f"[TOOL RESULT: Busy is: {_spoken_day(day)} from 10:00 AM to 11:00 AM.]"]
