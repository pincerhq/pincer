"""The reliability suite — 10 scenarios × 2 languages (Sprint 1 + Sprint 2).

Acceptance: ≥ 90% task-completion-or-graceful-failure in BOTH languages,
zero stuck calls (every scenario terminates COMPLETED/FAILED with cleanup),
and the initiating user always receives a final status message.
"""

from __future__ import annotations

import pytest

from .personas import (
    ConfusedPersona,
    CooperativePersona,
    HangsUpMidCallPersona,
    HostilePersona,
    InterruptingPersona,
    MumblerPersona,
    SilentPersona,
    VoicemailPersona,
    WrongNumberPersona,
)
from .personas_de import (
    ConfusedPersonaDe,
    CooperativePersonaDe,
    HangsUpMidCallPersonaDe,
    HostilePersonaDe,
    InterruptingPersonaDe,
    MumblerPersonaDe,
    SilentPersonaDe,
    VoicemailPersonaDe,
    WrongNumberPersonaDe,
)
from .report import render_report
from .runner import Scenario, ScenarioResult, run_scenario

SCENARIOS_EN = [
    Scenario("cooperative", CooperativePersona, expects_task_done=True),
    Scenario("confused", ConfusedPersona, expects_task_done=True),
    Scenario("interrupting", InterruptingPersona, expects_task_done=True),
    Scenario("hostile", HostilePersona),
    Scenario("wrong_number", WrongNumberPersona),
    Scenario("silent_timeout", SilentPersona),
    Scenario("voicemail", VoicemailPersona),
    Scenario("hangs_up_mid_call", HangsUpMidCallPersona),
    Scenario("mumbler", MumblerPersona, expects_task_done=True),
    Scenario("brain_errors", CooperativePersona, agent_fail_times=2),
]

SCENARIOS_DE = [
    Scenario("cooperative_de", CooperativePersonaDe, expects_task_done=True, language="de"),
    Scenario("confused_de", ConfusedPersonaDe, expects_task_done=True, language="de"),
    Scenario("interrupting_de", InterruptingPersonaDe, expects_task_done=True, language="de"),
    Scenario("hostile_de", HostilePersonaDe, language="de"),
    Scenario("wrong_number_de", WrongNumberPersonaDe, language="de"),
    Scenario("silent_timeout_de", SilentPersonaDe, language="de"),
    Scenario("voicemail_de", VoicemailPersonaDe, language="de"),
    Scenario("hangs_up_mid_call_de", HangsUpMidCallPersonaDe, language="de"),
    Scenario("mumbler_de", MumblerPersonaDe, expects_task_done=True, language="de"),
    Scenario("brain_errors_de", CooperativePersonaDe, agent_fail_times=2, language="de"),
]

SCENARIOS = SCENARIOS_EN + SCENARIOS_DE


async def _run_all(scenarios: list[Scenario]) -> list[ScenarioResult]:
    return [await run_scenario(scenario) for scenario in scenarios]


@pytest.mark.parametrize(
    "scenarios",
    [SCENARIOS_EN, SCENARIOS_DE],
    ids=["english", "german"],
)
async def test_suite_meets_reliability_target(scenarios):
    results = await _run_all(scenarios)
    report = render_report(results)
    passed = sum(1 for r in results if r.ok)
    assert passed / len(results) >= 0.9, f"Reliability below 90%:\n{report}"


async def test_zero_stuck_calls():
    results = await _run_all(SCENARIOS)
    for r in results:
        assert r.terminal_phase in ("CallPhase.COMPLETED", "CallPhase.FAILED", "completed", "failed"), (
            f"{r.name} did not terminate: {r.terminal_phase}"
        )
        assert r.call_cleaned_up, f"{r.name} left the call registered on the engine"


async def test_user_always_gets_final_status():
    results = await _run_all(SCENARIOS)
    for r in results:
        assert r.final_status_sent, f"{r.name}: no final status message reached the user"
        assert len(r.status_messages) <= 3, f"{r.name}: status spam ({len(r.status_messages)} messages)"


async def test_no_unverified_completion_claims():
    results = await _run_all(SCENARIOS)
    for r in results:
        assert not r.unverified_claims, f"{r.name}: agent claimed completion without a tool result"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_each_scenario_passes(scenario):
    result = await run_scenario(scenario)
    assert result.ok, f"{scenario.name} failed: {result}"


async def test_german_calls_speak_german_canned_lines():
    """A German call's timeout exit must be spoken in German — no English leakage."""
    result = await run_scenario(Scenario("silent_de_check", SilentPersonaDe, language="de"))
    assert result.ok
    assert any("Auf Wiederhören" in line for line in result.spoken), result.spoken
    assert not any("Goodbye" in line for line in result.spoken), result.spoken


async def test_german_brain_error_apology_is_german():
    result = await run_scenario(Scenario("brain_de_check", CooperativePersonaDe, agent_fail_times=2, language="de"))
    assert result.ok
    assert any("technische" in line for line in result.spoken), result.spoken
    assert not any("Goodbye" in line for line in result.spoken), result.spoken


async def test_report_renders():
    results = await _run_all(SCENARIOS)
    report = render_report(results)
    assert "# Voice Reliability Report" in report
    for scenario in SCENARIOS:
        assert scenario.name in report
