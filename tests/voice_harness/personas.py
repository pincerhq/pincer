"""Simulated callee personas for the voice reliability harness.

Each persona is a deterministic, scripted callee driven by the agent's last
utterance — cooperative, confused, interrupting, hostile, wrong-number, and
the failure shapes (silence, voicemail, mid-call hangup, garbled speech).
Deterministic scripts keep CI stable; an LLM-roleplay variant can reuse the
same interface for exploratory runs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PersonaAction:
    """What the callee does next: say something, hang up, or stay silent."""

    kind: str  # "say" | "hangup" | "silence"
    text: str = ""


def say(text: str) -> PersonaAction:
    return PersonaAction(kind="say", text=text)


HANGUP = PersonaAction(kind="hangup")
SILENCE = PersonaAction(kind="silence")


class SimulatedCallee:
    """Base persona: answers the phone and reacts to agent utterances."""

    name = "base"

    def __init__(self) -> None:
        self.turn = 0

    def opening(self) -> PersonaAction:
        """What the callee says when they pick up."""
        return say("Hello?")

    def react(self, agent_text: str) -> PersonaAction:
        """Scripted reaction to the agent's last utterance."""
        self.turn += 1
        return self._react(agent_text.lower())

    def _react(self, agent_text: str) -> PersonaAction:
        raise NotImplementedError


class CooperativePersona(SimulatedCallee):
    """Answers questions and confirms the appointment on the first ask."""

    name = "cooperative"

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text:
            return HANGUP
        if "confirm" in agent_text or "is that correct" in agent_text or "sound good" in agent_text:
            return say("Yes, that's correct, Tuesday at three works.")
        if "?" in agent_text:
            return say("Sure, go ahead.")
        return say("Okay.")


class ConfusedPersona(SimulatedCallee):
    """Doesn't understand who's calling for two turns, then cooperates."""

    name = "confused"

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text:
            return HANGUP
        if self.turn <= 2:
            return say("Wait, who is this? What is this about?")
        if "confirm" in agent_text or "is that correct" in agent_text:
            return say("Oh, I see. Yes, that's fine.")
        return say("Alright, I understand now.")


class InterruptingPersona(SimulatedCallee):
    """Talks over the agent (barge-in) before settling down and confirming."""

    name = "interrupting"

    def __init__(self) -> None:
        super().__init__()
        self.interruptions = 0

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text:
            return HANGUP
        if self.turn <= 2:
            self.interruptions += 1
            return say("—sorry, hold on, yes yes, about the appointment, right?")
        if "confirm" in agent_text or "is that correct" in agent_text:
            return say("Yes. Confirmed.")
        return say("Go on.")


class HostilePersona(SimulatedCallee):
    """Wants the call to end. The agent must exit politely, not argue."""

    name = "hostile"

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text or "sorry" in agent_text:
            return HANGUP
        return say("Stop calling me. I'm not interested. Remove this number.")


class WrongNumberPersona(SimulatedCallee):
    """The target person doesn't exist here; the agent must apologize and end."""

    name = "wrong_number"

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text:
            return HANGUP
        return say("There's no one by that name here. You have the wrong number.")


class SilentPersona(SimulatedCallee):
    """Picks up and says nothing — exercises the phase-timeout watchdog."""

    name = "silent"

    def opening(self) -> PersonaAction:
        return SILENCE

    def _react(self, agent_text: str) -> PersonaAction:
        return SILENCE


class VoicemailPersona(SimulatedCallee):
    """A voicemail greeting; the agent should leave a brief message and end."""

    name = "voicemail"

    def opening(self) -> PersonaAction:
        return say("You've reached the voicemail of the Schmidt family. Please leave a message after the tone. Beep.")

    def _react(self, agent_text: str) -> PersonaAction:
        return SILENCE


class HangsUpMidCallPersona(SimulatedCallee):
    """Engages for one turn, then hangs up mid-conversation."""

    name = "hangs_up_mid_call"

    def _react(self, agent_text: str) -> PersonaAction:
        if self.turn >= 2:
            return HANGUP
        return say("Yes, this is about the appointment, but actually—")


class MumblerPersona(SimulatedCallee):
    """Garbled first answers (low STT confidence); clarifies when asked to repeat."""

    name = "mumbler"

    def __init__(self) -> None:
        super().__init__()
        self.asked_to_repeat = 0

    def _react(self, agent_text: str) -> PersonaAction:
        if "goodbye" in agent_text:
            return HANGUP
        if "repeat" in agent_text or "say that again" in agent_text or "didn't catch" in agent_text:
            self.asked_to_repeat += 1
            return say("Sorry — yes, Tuesday at three o'clock is fine.")
        if self.turn <= 1:
            return say("mmph hrrm uhh whzz")
        if "confirm" in agent_text or "is that correct" in agent_text:
            return say("Yes, correct.")
        return say("Okay.")
