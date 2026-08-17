"""Deterministic agent brain for the harness.

Pursues one fixed task — confirming a dentist appointment for Tuesday 3pm —
with keyword logic instead of an LLM, so CI exercises the surrounding
machinery (channel, state machine, timeouts, cleanup, truthfulness) with
stable behavior. English and German variants (Sprint 2); the goodbye marker
("goodbye" / "auf wiederhören") signals the runner to hang up agent-side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.channels.base import IncomingMessage

GARBLED_MARKERS = ("mmph", "hrrm", "whzz", "uhh")

GOODBYE_MARKERS = ("goodbye", "auf wiederhören", "tschüss")


def said_goodbye(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in GOODBYE_MARKERS)


class ScriptedAgent:
    """Callable message handler with per-call task memory."""

    def __init__(self, fail_times: int = 0, language: str = "en") -> None:
        self.confirmed = False
        self.turns = 0
        self.language = language
        self._fail_times = fail_times

    async def __call__(self, incoming: IncomingMessage) -> str:
        self.turns += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("simulated LLM failure")

        text = incoming.text.lower()
        if self.language == "de":
            return self._respond_de(text)
        return self._respond_en(text)

    def _respond_en(self, text: str) -> str:
        if "wrong number" in text or "no one by that name" in text:
            return "I'm so sorry for the disturbance — I have the wrong number. Have a good day, goodbye."

        if "stop calling" in text or "not interested" in text or "remove this number" in text:
            return "I apologize for the inconvenience. I won't keep you. Goodbye."

        if "leave a message" in text or "voicemail" in text:
            return (
                "Hello, this is a message on behalf of Alex Miller about the dentist "
                "appointment on Tuesday at three. Please call back to confirm. Goodbye."
            )

        if any(marker in text for marker in GARBLED_MARKERS):
            return "Sorry, I didn't catch that — could you repeat that, please?"

        if "who is this" in text or "what is this about" in text:
            return (
                "Of course — I'm an assistant calling on behalf of Alex Miller about "
                "confirming a dentist appointment. Is Tuesday at three still okay?"
            )

        if self.confirmed:
            return "Thank you very much, that's everything. Have a great day, goodbye."

        if "yes" in text or "correct" in text or "confirmed" in text or "works" in text or "fine" in text:
            self.confirmed = True
            return "Great, so the appointment on Tuesday at three is confirmed. Thanks a lot — goodbye."

        return "I'm calling to confirm the dentist appointment on Tuesday at three. Is that correct?"

    def _respond_de(self, text: str) -> str:
        if "falsch verbunden" in text or "niemanden mit dem namen" in text or "kenne ich nicht" in text:
            return (
                "Oh, entschuldigen Sie vielmals — da bin ich falsch verbunden. Einen schönen Tag noch, auf Wiederhören."
            )

        if "rufen sie nicht" in text or "kein interesse" in text or "nummer löschen" in text:
            return "Entschuldigen Sie bitte die Störung. Ich halte Sie nicht länger auf. Auf Wiederhören."

        if "nachricht" in text and ("hinterlassen" in text or "signalton" in text or "mailbox" in text):
            return (
                "Guten Tag, hier ist eine Nachricht im Auftrag von Alex Müller wegen des "
                "Zahnarzttermins am Dienstag um fünfzehn Uhr. Bitte rufen Sie zur Bestätigung zurück. Auf Wiederhören."
            )

        if any(marker in text for marker in GARBLED_MARKERS):
            return "Entschuldigung, das habe ich nicht verstanden — können Sie das bitte wiederholen?"

        if "wer ist da" in text or "wer spricht" in text or "worum geht" in text:
            return (
                "Natürlich — ich bin ein Assistent und rufe im Auftrag von Alex Müller an, es geht "
                "um die Bestätigung eines Zahnarzttermins. Passt Dienstag um fünfzehn Uhr weiterhin?"
            )

        if self.confirmed:
            return "Vielen Dank, das war alles. Einen schönen Tag noch, auf Wiederhören."

        if "ja" in text.split() or "passt" in text or "genau" in text or "in ordnung" in text or "stimmt" in text:
            self.confirmed = True
            return "Sehr gut, dann ist der Termin am Dienstag um fünfzehn Uhr bestätigt. Vielen Dank — auf Wiederhören."

        return "Ich rufe an, um den Zahnarzttermin am Dienstag um fünfzehn Uhr zu bestätigen. Ist das so richtig?"
