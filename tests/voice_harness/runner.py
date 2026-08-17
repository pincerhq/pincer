"""Scenario runner for the voice reliability harness.

Drives a full simulated call through the real VoiceChannel + CallStateMachine
against a FakeVoiceEngine and a scripted persona, then checks the Sprint 1
reliability contract: terminal state machine, engine cleanup, a final status
message to the initiating user, and no unverified completion claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import status_notify
from pincer.voice.engine import CallDirection
from pincer.voice.state_machine import PHASE_TIMEOUTS

from .fake_engine import FakeVoiceEngine
from .scripted_agent import ScriptedAgent, said_goodbye

if TYPE_CHECKING:
    from .personas import SimulatedCallee

MAX_TURNS = 12


@dataclass
class Scenario:
    name: str
    persona_factory: type[SimulatedCallee]
    expects_task_done: bool = False
    agent_fail_times: int = 0
    language: str = "en"


@dataclass
class ScenarioResult:
    name: str
    ok: bool = False
    task_done: bool = False
    terminal_phase: str = ""
    turns: int = 0
    call_cleaned_up: bool = False
    final_status_sent: bool = False
    unverified_claims: list[str] = field(default_factory=list)
    status_messages: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    mean_turn_latency_s: float | None = None
    notes: list[str] = field(default_factory=list)


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de"
    settings.voice_de_formality = "sie"
    return settings


async def run_scenario(scenario: Scenario, post_call_processor=None) -> ScenarioResult:
    result = ScenarioResult(name=scenario.name)
    call_sid = f"CA_harness_{scenario.name}"

    status_notify._reset_for_tests()
    captured_status: list[str] = []

    async def _capture_notifier(user_id: str, channel: str, text: str) -> bool:
        captured_status.append(text)
        return True

    status_notify.set_status_notifier(_capture_notifier)
    status_notify.register_outbound_call(call_sid, user_id="tester", channel="telegram", target_number="+15550001111")

    settings = _make_settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    if post_call_processor is not None:
        channel.set_post_call_processor(post_call_processor)
    agent = ScriptedAgent(fail_times=scenario.agent_fail_times, language=scenario.language)
    await channel.start(agent)

    persona = scenario.persona_factory()
    state = await engine.on_call_start(
        call_sid,
        "+15550001111",
        CallDirection.OUTBOUND,
        language=scenario.language,
    )
    sm = channel._ensure_call_tracking(call_sid, state)
    transcript = channel.get_transcript(call_sid)
    metrics = channel.metrics.get(call_sid)

    action_logged = False

    def _sync_task_state() -> None:
        nonlocal action_logged
        # The runner plays the tool layer: when the scripted agent commits the
        # task, a successful CallAction is recorded (backs completion claims).
        if agent.confirmed and not action_logged and transcript:
            transcript.log_action("tool_call", "calendar_confirm", output_summary="Appointment confirmed")
            action_logged = True

    try:
        action = persona.opening()
        for _ in range(MAX_TURNS):
            if call_sid in engine.ended:
                break

            if action.kind == "hangup":
                await engine.end_call(call_sid)
                break

            if action.kind == "silence":
                # Nothing will ever arrive: fast-forward past the phase timeout
                # and run one watchdog pass (identical to the live loop's check).
                sm._state.phase_entered_at -= PHASE_TIMEOUTS.get(sm.phase, 60) + 1
                if sm.check_timeout():
                    await channel._handle_phase_timeout(call_sid, sm)
                break

            spoken_before = len(engine.spoken.get(call_sid, []))
            await channel._handle_speech(call_sid, action.text)
            result.turns += 1
            _sync_task_state()

            new_utterances = engine.spoken.get(call_sid, [])[spoken_before:]
            agent_text = new_utterances[-1] if new_utterances else ""

            if call_sid in engine.ended:
                break
            if said_goodbye(agent_text):
                await engine.end_call(call_sid)
                break

            action = persona.react(agent_text) if agent_text else persona.react("")

        if call_sid not in engine.ended:
            result.notes.append("max_turns_reached")
            await engine.end_call(call_sid)
    finally:
        await channel.stop()
        status_notify.set_status_notifier(None)

    result.task_done = agent.confirmed
    result.spoken = list(engine.spoken.get(call_sid, []))
    result.terminal_phase = str(sm.phase)
    result.call_cleaned_up = call_sid not in engine.get_active_calls()
    result.status_messages = list(captured_status)
    # Final status = any message past the dialing/connected stages (the plain
    # "📞 Call ended" line, or the Sprint 3 structured report replacing it)
    result.final_status_sent = any(
        not msg.startswith("📞 Dialing") and msg != "📞 Connected" for msg in captured_status
    )
    if transcript:
        result.unverified_claims = transcript.verify_completion_claims()
    if metrics:
        result.mean_turn_latency_s = metrics.mean_turn_latency_s

    honest = not result.unverified_claims
    graceful = sm.is_terminal and result.call_cleaned_up and result.final_status_sent and honest
    if scenario.expects_task_done:
        result.ok = graceful and result.task_done
    else:
        result.ok = graceful
    if "max_turns_reached" in result.notes:
        result.ok = False
    return result
