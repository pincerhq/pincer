"""German-speaking counterparts of the harness personas (Sprint 2, T2.6).

Same behavioral shapes as personas.py, reacting to the German scripted agent's
keywords and speaking German — so the full suite runs in both languages.
"""

from __future__ import annotations

from .personas import HANGUP, SILENCE, PersonaAction, SimulatedCallee, say


def _agent_said_goodbye(agent_text: str) -> bool:
    return "auf wiederhören" in agent_text or "tschüss" in agent_text


class CooperativePersonaDe(SimulatedCallee):
    name = "cooperative_de"

    def opening(self) -> PersonaAction:
        return say("Hallo?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text):
            return HANGUP
        if "richtig" in agent_text or "passt" in agent_text or "bestätigen" in agent_text:
            return say("Ja, genau, Dienstag um fünfzehn Uhr passt.")
        if "?" in agent_text:
            return say("Ja, gerne.")
        return say("In Ordnung.")


class ConfusedPersonaDe(SimulatedCallee):
    name = "confused_de"

    def opening(self) -> PersonaAction:
        return say("Hallo? Ja bitte?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text):
            return HANGUP
        if self.turn <= 2:
            return say("Moment, wer ist da bitte? Worum geht es denn?")
        if "richtig" in agent_text or "passt" in agent_text:
            return say("Ach so, verstehe. Ja, das passt.")
        return say("Alles klar, jetzt verstehe ich.")


class InterruptingPersonaDe(SimulatedCallee):
    name = "interrupting_de"

    def __init__(self) -> None:
        super().__init__()
        self.interruptions = 0

    def opening(self) -> PersonaAction:
        return say("Hallo?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text):
            return HANGUP
        if self.turn <= 2:
            self.interruptions += 1
            return say("— Moment, ja ja, es geht um den Termin, richtig? Genau.")
        if "richtig" in agent_text or "passt" in agent_text:
            return say("Ja. Passt.")
        return say("Weiter bitte.")


class HostilePersonaDe(SimulatedCallee):
    name = "hostile_de"

    def opening(self) -> PersonaAction:
        return say("Ja, was ist?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text) or "entschuldig" in agent_text:
            return HANGUP
        return say("Rufen Sie nicht mehr an. Kein Interesse. Nummer löschen bitte.")


class WrongNumberPersonaDe(SimulatedCallee):
    name = "wrong_number_de"

    def opening(self) -> PersonaAction:
        return say("Hallo?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text):
            return HANGUP
        return say("Hier gibt es niemanden mit dem Namen. Sie sind falsch verbunden.")


class SilentPersonaDe(SimulatedCallee):
    name = "silent_de"

    def opening(self) -> PersonaAction:
        return SILENCE

    def _react(self, agent_text: str) -> PersonaAction:
        return SILENCE


class VoicemailPersonaDe(SimulatedCallee):
    name = "voicemail_de"

    def opening(self) -> PersonaAction:
        return say(
            "Hier ist der Anrufbeantworter der Familie Schmidt. "
            "Bitte hinterlassen Sie eine Nachricht nach dem Signalton. Piep."
        )

    def _react(self, agent_text: str) -> PersonaAction:
        return SILENCE


class HangsUpMidCallPersonaDe(SimulatedCallee):
    name = "hangs_up_mid_call_de"

    def opening(self) -> PersonaAction:
        return say("Hallo?")

    def _react(self, agent_text: str) -> PersonaAction:
        if self.turn >= 2:
            return HANGUP
        return say("Ja, es geht um den Termin, aber eigentlich—")


class MumblerPersonaDe(SimulatedCallee):
    name = "mumbler_de"

    def __init__(self) -> None:
        super().__init__()
        self.asked_to_repeat = 0

    def opening(self) -> PersonaAction:
        return say("Hallo?")

    def _react(self, agent_text: str) -> PersonaAction:
        if _agent_said_goodbye(agent_text):
            return HANGUP
        if "wiederholen" in agent_text or "nicht verstanden" in agent_text:
            self.asked_to_repeat += 1
            return say("Entschuldigung — ja, Dienstag um fünfzehn Uhr passt gut.")
        if self.turn <= 1:
            return say("mmph hrrm uhh whzz")
        if "richtig" in agent_text or "passt" in agent_text:
            return say("Ja, genau.")
        return say("In Ordnung.")
