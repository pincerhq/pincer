"""Intent token parsing (Sprint 12 §7.2).

In RECEPTION_INTENT the conversation LLM is instructed to start its reply
with exactly one line ``[INTENT:question|message|appointment|human|unknown]``.
The token is machine-parsed and stripped before TTS — the same mechanism as
``[SWITCH_LANGUAGE:xx]`` (language_guard) and ``[APPOINTMENT_CONFIRMED:…]``
(scheduling). Anything that is not one of the five values is ``unknown``.
"""

from __future__ import annotations

import re

INTENT_QUESTION = "question"
INTENT_MESSAGE = "message"
INTENT_APPOINTMENT = "appointment"
INTENT_HUMAN = "human"
INTENT_UNKNOWN = "unknown"
INTENT_AFTER_HOURS = "after_hours"  # persisted value for after-hours calls (never emitted by the LLM)

INTENTS: tuple[str, ...] = (INTENT_QUESTION, INTENT_MESSAGE, INTENT_APPOINTMENT, INTENT_HUMAN, INTENT_UNKNOWN)

INTENT_TOKEN_RE = re.compile(r"\[\s*INTENT\s*:\s*([A-Za-z_]+)\s*\]", re.IGNORECASE)

# The primary-intent rule for mixed requests (§7.2): the actionable one wins.
_PRIORITY = {INTENT_HUMAN: 0, INTENT_APPOINTMENT: 1, INTENT_MESSAGE: 2, INTENT_QUESTION: 3, INTENT_UNKNOWN: 4}


def parse_intent_token(text: str) -> tuple[str | None, str]:
    """(intent or None when no token, text with every token stripped).

    Several tokens (a confused model) resolve to the most actionable one; an
    unrecognized value is ``unknown`` — never silently ignored.
    """
    matches = INTENT_TOKEN_RE.findall(text or "")
    if not matches:
        return None, text or ""
    values = [m.strip().lower() for m in matches]
    normalized = [v if v in INTENTS else INTENT_UNKNOWN for v in values]
    intent = min(normalized, key=lambda v: _PRIORITY[v])
    stripped = INTENT_TOKEN_RE.sub("", text or "")
    stripped = re.sub(r"^\s*\n", "", stripped).strip()
    return intent, stripped


__all__ = [
    "INTENTS",
    "INTENT_AFTER_HOURS",
    "INTENT_APPOINTMENT",
    "INTENT_HUMAN",
    "INTENT_MESSAGE",
    "INTENT_QUESTION",
    "INTENT_TOKEN_RE",
    "INTENT_UNKNOWN",
    "parse_intent_token",
]
