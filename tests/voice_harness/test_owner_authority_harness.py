"""Issue #252 harness — an outbound call acts on the owner's authority.

The owner asks for a call (pizza order, a delivery slot); the callee is an
unregistered business, so a guest. The in-call tools run for real — the
Pincer call tools on a real ToolRegistry, through the real gate — and the
assertions are about where their results land: the owner's memory under the
canonical id, the chat session that asked for the call, the owner's approval
card. Nobody ever asks the person on the phone to approve the owner's actions.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from pincer.channels.phone_calls import VoiceChannel
from pincer.tools.approval import ApprovalPolicy
from pincer.tools.registry import ToolRegistry
from pincer.voice import approvals, scheduling, status_notify
from pincer.voice import in_call_tools as ict
from pincer.voice import tool_policy as tp
from pincer.voice.call_tools import register_call_tools
from pincer.voice.engine import CallDirection
from pincer.voice.postcall import PostCallProcessor

from .fake_engine import FakeVoiceEngine
from .settings import apply_in_call_tool_defaults, apply_test_paths
from .tool_agent import Tool, ToolAgent

CALL = "CA_owner_harness"
PIZZERIA = "+4930555000"
SESSION = "telegram:12345"
CREATE_ARGS = {"summary": "Pizza-Lieferung", "start": "2026-11-12T19:30:00+01:00", "end": "2026-11-12T19:45:00+01:00"}


class RegistryToolAgent(ToolAgent):
    """A scripted model whose tool calls run the REAL registry handlers."""

    def __init__(self, turns, registry: ToolRegistry) -> None:
        super().__init__(turns)
        self.registry = registry

    def _executor(self, step: Tool):
        async def _run() -> tuple[str, bool]:
            self.executed.append((step.name, dict(step.args)))
            result = await self.registry.execute(step.name, dict(step.args), context={"pincer_user_id": PIZZERIA})
            return result, result.startswith("Error")

        return _run


class _Memory:
    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    async def store_memory(self, **kwargs: Any) -> str:
        self.stored.append(kwargs)
        return f"m{len(self.stored)}"

    async def search_text(self, query, user_id=None, limit=5, tags=None):
        return []


def _settings(**overrides: Any) -> Any:
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "de-DE"
    settings.voice_default_language = "de"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_filler_phrases = ""
    settings.voice_approval_timeout_s = 5
    settings.data_dir = None
    return apply_in_call_tool_defaults(settings, **overrides)


@pytest.fixture(autouse=True)
def _clean():
    scheduling._reset_for_tests()
    status_notify._reset_for_tests()
    approvals._reset_for_tests()
    yield
    scheduling._reset_for_tests()
    status_notify._reset_for_tests()
    approvals._reset_for_tests()


@pytest.fixture
def owner_chat():
    """The owner's channel and the chat session that asked for the call."""
    pushed: list[tuple[str, str, str]] = []
    notes: list[tuple[str, str, str, str]] = []

    async def notifier(user_id, channel, text):
        pushed.append((user_id, channel, text))
        return True

    async def reporter(pincer_user_id, channel, session_key, text):
        notes.append((pincer_user_id, channel, session_key, text))

    status_notify.set_status_notifier(notifier)
    status_notify.set_session_reporter(reporter)
    return pushed, notes


def _registry(memory: _Memory, policy: ApprovalPolicy | None = None, calendar: list | None = None) -> ToolRegistry:
    registry = ToolRegistry(approval_policy=policy) if policy is not None else ToolRegistry()
    register_call_tools(registry, MagicMock(db_path=""), memory=memory)
    if calendar is not None:

        async def google__create_event(**kwargs: Any) -> str:
            kwargs.pop("context", None)
            calendar.append(kwargs)
            return f"Event created: '{kwargs.get('summary')}'\nID: ev{len(calendar)}"

        registry.register(
            name="google__create_event",
            description="Create a calendar event",
            handler=google__create_event,
            parameters={"type": "object", "properties": {}},
            require_approval=True,
        )
    return registry


async def _start(agent: ToolAgent, registry: ToolRegistry, settings: Any, grants=frozenset()):
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    channel.set_stream_agent(agent)
    channel.set_tool_registry(registry)
    await channel.start(lambda incoming: None)
    # What make_phone_call tracks for a call placed from the owner's Telegram chat
    status_notify.register_outbound_call(
        CALL,
        user_id="12345",
        channel="telegram",
        language="de",
        target_number=PIZZERIA,
        target_name="Pizzeria Roma",
        pincer_user_id="owner-1",
        session_key=SESSION,
        tool_grants=grants,
    )
    state = await engine.on_call_start(CALL, PIZZERIA, CallDirection.OUTBOUND, language="de")
    return channel, engine, state


def _verify_lines(engine: FakeVoiceEngine) -> list[str]:
    """The spoken "please confirm" questions (the de verify opener)."""
    return [line for line in engine.spoken[CALL] if line.startswith("Zur Bestätigung")]


async def test_pizza_delivery_change_reaches_the_owner_without_asking_the_restaurant(owner_chat):
    pushed, notes = owner_chat
    memory = _Memory()
    registry = _registry(memory)
    agent = RegistryToolAgent(
        [
            [
                Tool("memory_note", {"note": "Pizzeria Roma liefert um 19:30 statt 19:00."}),
                Tool(
                    "send_owner_message",
                    {"text": "Die Pizzeria liefert erst um 19:30."},
                    then="Alles klar, dann 19:30. Danke!",
                ),
            ]
        ],
        registry,
    )
    channel, engine, state = await _start(agent, registry, _settings())
    assert state.metadata[tp.META_CALLER_CLASS] == tp.CALLER_GUEST
    assert state.metadata[tp.META_PRINCIPAL] == tp.PRINCIPAL_OWNER

    await engine.on_speech_input(CALL, "Wir schaffen die Lieferung erst um halb acht.")
    await status_notify.drain(1.0)

    assert [r.executed for r in agent.gate_results] == [True, True]
    assert _verify_lines(engine) == []  # nobody asked the restaurant to confirm
    # The note: the owner's canonical id, tagged with the call and the chat
    assert memory.stored[0]["user_id"] == "owner-1"
    assert {f"call:{CALL}", f"session:{SESSION}"} <= set(memory.stored[0]["extra_tags"])
    # The message: pushed to the owner's channel AND in the chat that asked
    assert pushed == [("12345", "telegram", "📞 Die Pizzeria liefert erst um 19:30.")]
    assert notes == [
        (
            "owner-1",
            "telegram",
            SESSION,
            '📞 Message relayed during the call with Pizzeria Roma: "Die Pizzeria liefert erst um 19:30."',
        )
    ]
    # Ran without anyone confirming → disclosed in the owner's post-call report
    report = PostCallProcessor._render_autonomous_actions(channel.get_transcript(CALL), "de")
    assert "memory_note" in report and "send_owner_message" in report
    await channel.stop()


async def test_callee_saying_no_cannot_drop_the_owners_message(owner_chat):
    """The restaurant's 'no' is not an answer to the owner's actions."""
    pushed, _notes = owner_chat
    memory = _Memory()
    registry = _registry(memory)
    agent = RegistryToolAgent(
        [[Tool("send_owner_message", {"text": "Margherita ist aus, nur noch Salami."})]],
        registry,
    )
    channel, engine, state = await _start(agent, registry, _settings())
    await engine.on_speech_input(CALL, "Nein, nein, sagen Sie ihm nichts.")
    assert agent.gate_results[0].executed
    assert pushed and pushed[0][2] == "📞 Margherita ist aus, nur noch Salami."
    await channel.stop()


async def test_granted_calendar_write_runs_under_the_owners_policy_and_is_disclosed(owner_chat):
    calendar: list[dict[str, Any]] = []
    registry = _registry(_Memory(), calendar=calendar)
    agent = RegistryToolAgent([[Tool("google__create_event", CREATE_ARGS, then="Eingetragen.")]], registry)
    channel, engine, state = await _start(agent, registry, _settings(), grants=frozenset({"google__create_event"}))
    await engine.on_speech_input(CALL, "Die Lieferung kommt Donnerstag um halb acht.")
    assert [c["summary"] for c in calendar] == ["Pizza-Lieferung"]
    assert _verify_lines(engine) == []
    report = PostCallProcessor._render_autonomous_actions(channel.get_transcript(CALL), "de")
    assert "google__create_event" in report
    await channel.stop()


async def test_ungranted_calendar_write_is_out_of_scope(owner_chat):
    calendar: list[dict[str, Any]] = []
    registry = _registry(_Memory(), calendar=calendar)
    agent = RegistryToolAgent([[Tool("google__create_event", CREATE_ARGS)]], registry)
    channel, engine, state = await _start(agent, registry, _settings())
    await engine.on_speech_input(CALL, "Tragen Sie das ein.")
    assert agent.gate_results[0].deny_reason == tp.REASON_NOT_IN_SCOPE
    assert calendar == []
    await channel.stop()


async def test_granted_calendar_write_under_a_strict_policy_waits_for_the_owners_card(owner_chat, monkeypatch):
    monkeypatch.setattr(ict, "HOLD_REASSURE_INTERVAL_S", 0.05)
    cards: list[Any] = []

    async def presenter(req):
        cards.append(req)
        return True

    approvals.set_presenter(presenter)
    calendar: list[dict[str, Any]] = []
    registry = _registry(_Memory(), policy=ApprovalPolicy(mode="all"), calendar=calendar)
    agent = RegistryToolAgent([[Tool("google__create_event", CREATE_ARGS, then="Eingetragen.")]], registry)
    channel, engine, state = await _start(agent, registry, _settings(), grants=frozenset({"google__create_event"}))

    async def _owner_approves():
        while not cards:
            await asyncio.sleep(0.01)
        assert approvals.resolve(cards[0].approval_id, True, by_user_id="12345")

    approver = asyncio.create_task(_owner_approves())
    await engine.on_speech_input(CALL, "Die Lieferung kommt Donnerstag um halb acht.")
    await approver
    assert (cards[0].user_id, cards[0].channel) == ("12345", "telegram")
    assert [c["summary"] for c in calendar] == ["Pizza-Lieferung"]
    assert _verify_lines(engine) == []
    action = [a for a in channel.get_transcript(CALL).actions if a.action_type == "tool_execute"][-1]
    assert action.approval_mode == "user" and action.user_confirmed is True
    await channel.stop()
