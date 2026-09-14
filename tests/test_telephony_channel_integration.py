"""
Instrumentation exercised through the REAL VoiceChannel.

Everything else tests the telemetry layer in isolation. This drives whole
simulated calls through `VoiceChannel` + `CallStateMachine` against the voice
harness's fake engine and scripted agent, so the hooks themselves — the ones
actually wired into `_handle_speech_turn`, `_run_streaming_turn` and
`_handle_call_end` — are what produce the rows being asserted.

If someone removes a hook, these fail; the isolated tests would not notice.
"""

from __future__ import annotations

import pytest
from voice_harness.personas import CooperativePersona, HangsUpMidCallPersona
from voice_harness.runner import Scenario, _make_settings, run_scenario

from pincer.db import ensure_schema_current
from pincer.voice.telemetry import queries, runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.tracer import drain_pending


@pytest.fixture
async def telemetry(tmp_path):
    db_path = tmp_path / "telephony.db"
    ensure_schema_current(db_path)
    runtime.reset_for_tests(db_path=str(db_path))
    await get_recorder().start()
    yield db_path
    await runtime.shutdown()
    runtime.reset_for_tests()


async def _run(scenario: Scenario, db_path):
    """Register the call the way the inbound webhook does, then drive it."""
    settings = _make_settings(db_path)
    call_sid = f"CA_harness_{scenario.name}"
    tracer = runtime.start_call(
        provider_call_id=call_sid,
        # The harness runs `FakeVoiceEngine`, whose engine_name is "fake".
        # Registering it honestly is the point: the row must describe what ran.
        engine="fake",
        direction="outbound",
        language=scenario.language,
        to_number="+15550001111",
    )
    assert tracer is not None, "telemetry must be live for this test to mean anything"
    tracer.answered()

    result = await run_scenario(scenario, settings=settings)
    await drain_pending()
    await get_recorder().flush()
    return call_sid, result


async def test_a_driven_call_produces_call_turn_and_event_rows(telemetry):
    call_sid, result = await _run(
        Scenario(name="telemetry_ok", persona_factory=CooperativePersona, expects_task_done=True),
        telemetry,
    )
    assert result.turns > 0

    call = await queries.get_call(telemetry, call_sid)
    assert call is not None
    assert call["status"] in ("completed", "failed")
    assert call["ended_at"]
    # The channel's own turn counter and the telemetry rows must agree.
    assert call["turn_count"] == result.turns

    turns = await queries.get_turns(telemetry, call["call_id"])
    assert len(turns) == result.turns
    assert all(turn["engine"] == "fake" for turn in turns)

    events = await queries.get_events(telemetry, call["call_id"])
    names = {event["name"] for event in events}
    assert {"call.registered", "call.answered", "turn.start", "turn.end", "call.ended"} <= names

    # Lifecycle states are recorded as their own transitions, separate from the
    # pipeline spans — the streaming pipeline is not a state machine and must
    # not be flattened into one.
    phases = [event for event in events if event["name"] == "call.phase"]
    assert phases, "state machine transitions must reach the timeline"
    assert all(event["attributes"]["to_phase"] for event in phases)
    assert any(event["attributes"].get("reason") for event in phases)


async def test_turns_report_an_estimated_latency_source_without_speech_end(telemetry):
    """No engine-measured speech end means the clock starts at transcript arrival.

    That is the ConversationRelay shape, and the harness reproduces it: the
    number must be LABELLED as the estimate it is, not passed off as the real
    voice-to-voice latency.
    """
    call_sid, _ = await _run(
        Scenario(name="telemetry_source", persona_factory=CooperativePersona),
        telemetry,
    )
    call = await queries.get_call(telemetry, call_sid)
    turns = await queries.get_turns(telemetry, call["call_id"])
    assert turns
    assert all(turn["response_latency_source"] == "transcript_arrival" for turn in turns)


async def test_an_abrupt_hangup_still_leaves_a_diagnosable_call(telemetry):
    call_sid, _ = await _run(
        Scenario(name="telemetry_hangup", persona_factory=HangsUpMidCallPersona),
        telemetry,
    )
    call = await queries.get_call(telemetry, call_sid)
    assert call is not None
    assert call["ended_at"]
    assert call["failure_code"]  # whatever it was, it is recorded
    assert call["failure_category"] in (
        "none",
        "technical",
        "ended_by_party",
        "callee_unavailable",
        "unknown",
    )
    events = await queries.get_events(telemetry, call["call_id"])
    assert any(event["name"] == "call.ended" for event in events)


async def test_a_brain_error_marks_the_turn_failed_without_killing_the_call(telemetry):
    call_sid, _ = await _run(
        Scenario(name="telemetry_error", persona_factory=CooperativePersona, agent_fail_times=1),
        telemetry,
    )
    call = await queries.get_call(telemetry, call_sid)
    turns = await queries.get_turns(telemetry, call["call_id"])
    failed = [turn for turn in turns if turn["error"]]
    assert failed, "a scripted agent failure must appear on its turn"
    assert failed[0]["complete"] is False


async def test_telemetry_being_off_does_not_change_call_behaviour(tmp_path):
    """The call is what matters; the trace of it is not allowed to matter more."""
    runtime.reset_for_tests(db_path="", enabled_=False)
    settings = _make_settings(tmp_path / "off.db")
    result = await run_scenario(
        Scenario(name="telemetry_off", persona_factory=CooperativePersona, expects_task_done=True),
        settings=settings,
    )
    assert result.turns > 0
    assert result.terminal_phase
    assert runtime.tracer_for("CA_harness_telemetry_off") is None


# ── agent-level instrumentation ──────────────────────────────────────


class _ScriptedProvider:
    """Minimal `stream_turn` provider: scripted turns, no network."""

    def __init__(self, turns):
        self.turns = list(turns)

    async def complete(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("voice turns must not use stream()")
        yield ""

    async def stream_turn(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        for event in self.turns.pop(0):
            yield event

    async def close(self) -> None:
        pass

    def is_free(self, provider: str) -> bool:  # pragma: no cover - router parity
        return True


async def test_every_parallel_tool_call_is_traced(telemetry, settings, session_manager, cost_tracker, tool_registry):
    """Two tools in one LLM iteration must produce two spans and two events.

    Regression guard: recording tool starts as turn *stamps* silently kept only
    the first, so a turn that fanned out to three integrations looked like a
    turn that called one.
    """
    from pincer.core.agent import Agent
    from pincer.llm.base import LLMResponse, StreamTurnEvent, ToolCall
    from pincer.voice.telemetry.schema import SpanName
    from pincer.voice.telemetry.tracer import bind_turn_tracer

    async def fake_tool() -> str:
        return "ok"

    for name in ("calendar_today", "contact_lookup"):
        tool_registry.register(name, "d", fake_tool, {"type": "object", "properties": {}})

    def _turn(text="", tool_calls=None):
        events = [StreamTurnEvent(text=text)] if text else []
        events.append(
            StreamTurnEvent(
                response=LLMResponse(
                    content=text,
                    tool_calls=tool_calls or [],
                    model="test-model",
                    input_tokens=10,
                    output_tokens=5,
                )
            )
        )
        return events

    provider = _ScriptedProvider(
        [
            _turn(
                tool_calls=[
                    ToolCall(id="t1", name="calendar_today", arguments={}),
                    ToolCall(id="t2", name="contact_lookup", arguments={}),
                ]
            ),
            _turn("Beides erledigt."),
        ]
    )
    agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

    tracer = runtime.start_call(provider_call_id="CA_tools", direction="inbound", engine="media_streams")
    assert tracer is not None
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)

    with bind_turn_tracer(turn):
        async for _ in agent.stream_voice_turn(user_id="u1", channel="voice", text="Was steht an?"):
            pass

    turn.first_audio(kind="audio")
    turn.finish()
    await tracer.finish(status="completed", failure_code="none")
    await drain_pending()
    await get_recorder().flush()

    call = await queries.get_call(telemetry, "CA_tools")
    spans = await queries.get_spans(telemetry, call["call_id"])
    tool_spans = [s for s in spans if s["name"] == str(SpanName.TOOL)]
    assert {s["attributes"]["tool"] for s in tool_spans} == {"calendar_today", "contact_lookup"}

    events = await queries.get_events(telemetry, call["call_id"])
    starts = [e for e in events if e["name"] == "tool.start"]
    assert len(starts) == 2
    assert {e["attributes"]["tool"] for e in starts} == {"calendar_today", "contact_lookup"}

    rows = await queries.get_turns(telemetry, call["call_id"])
    assert rows[0]["tool_calls"] == 2
    # Two LLM iterations, each its own span.
    assert len([s for s in spans if s["name"] == str(SpanName.LLM)]) == 2
