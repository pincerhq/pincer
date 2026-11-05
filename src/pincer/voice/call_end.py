"""
Ending a call on goodbyes.

The model closes a finished conversation with a short farewell plus the
``[END_CALL]`` token; the token is stripped before TTS, the farewell plays,
and after PINCER_VOICE_HANGUP_GRACE_S the call is hung up. A farewell from
the other party is detected per language: after the agent already said
goodbye it ends the call directly (no further LLM turn); said first, the
next agent turn is a one-sentence goodbye. Anything said during the grace
window cancels the hangup.
"""

from __future__ import annotations

import re
from typing import Any

END_CALL_TOKEN = "[END_CALL]"
_END_TOKEN_RE = re.compile(r"\[\s*END[_ ]?CALL\s*\]", re.IGNORECASE)

# farewell phrases per language
_CORE: dict[str, str] = {
    "en": (
        r"good\s?bye|bye(?:\s?bye)?|see you(?: soon| later| then)?|take care|"
        r"talk (?:to you )?(?:later|soon)|have a (?:good|nice|great|lovely) (?:day|one|evening|weekend)|cheers"
    ),
    "de": (
        r"tsch[uü]ss(?:i)?|tschau|ciao|auf wiederh[öo]ren|auf wiedersehen|wiederh[öo]ren|wiedersehen|"
        r"bis dann|bis bald|bis sp[äa]ter|bis morgen|bis demn[äa]chst|mach'?s gut|machen sie'?s gut|"
        r"(?:einen )?sch[öo]nen (?:tag|abend|feierabend)(?: noch)?|sch[öo]nes wochenende|gute nacht"
    ),
    "uk": (
        r"до побачення|бувай(?:те)?|до зустрічі|до зв'?язку|гарного (?:дня|вечора)|на все добре|"
        r"всього (?:найкращого|доброго)|щасливо|пока|па-?па"
    ),
}
# words that may accompany a farewell without making it more than a goodbye
_FILLER: dict[str, str] = {
    "en": (
        r"ok(?:ay)?|alright|all right|thanks?|thank you(?: very much| so much)?|great|perfect|good|sure|"
        r"yes|yeah|yep|no|that'?s (?:all|it)|nothing else|you too|and you|then|so|well|right|fine|cool"
    ),
    "de": (
        r"ok(?:ay)?|alles klar|danke(?: sch[öo]n| sehr| dir| ihnen)?|vielen dank|gut|super|prima|perfekt|"
        r"ja|nein|genau|das war'?s|das wars|nichts mehr|ebenso|gleichfalls|ihnen auch|dir auch|dann|also|gerne|"
        r"in ordnung|passt|na|naja|nun|so|das wäre alles|das ist alles"
    ),
    "uk": (
        r"добре|гаразд|ок(?:ей)?|дякую|дуже дякую|так|ні|чудово|супер|все|це все|більше нічого|вам теж|"
        r"навзаєм|тоді|ну|і вам|ага"
    ),
}
_MAX_FAREWELL_WORDS = 10

_compiled: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {}


def _lang(language: str) -> str:
    code = str(language or "en").strip().lower()[:2]
    return code if code in _CORE else "en"


def _patterns(language: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    lang = _lang(language)
    if lang not in _compiled:
        core = re.compile(rf"\b(?:{_CORE[lang]})\b", re.IGNORECASE)
        only = re.compile(rf"^(?:\b(?:{_CORE[lang]}|{_FILLER[lang]})\b[\s,.!?;:\-—…]*)+$", re.IGNORECASE)
        _compiled[lang] = (core, only)
    return _compiled[lang]


def _normalize(text: str) -> str:
    t = str(text or "").strip().lower().replace("’", "'")
    t = re.sub(r"[\"()\[\]«»]", " ", t)
    t = re.sub(r"[\s,.!?;:\-—…]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_end_call_token(text: str) -> tuple[bool, str]:
    """(token present, text with every token removed and whitespace tidied)."""
    if not text or "[" not in text:
        return False, text
    if not _END_TOKEN_RE.search(text):
        return False, text
    stripped = _END_TOKEN_RE.sub("", text)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped).strip()
    return True, stripped


def contains_farewell(text: str, language: str) -> bool:
    """True if the text contains a goodbye (used on the agent's own lines)."""
    core, _ = _patterns(language)
    return bool(core.search(_normalize(text)))


def is_farewell(text: str, language: str) -> bool:
    """True if the utterance is a goodbye and nothing more
    ("okay thanks, bye!" yes; "bye, but one more question" no)."""
    t = _normalize(text)
    if not t or len(t.split()) > _MAX_FAREWELL_WORDS:
        return False
    core, only = _patterns(language)
    return bool(core.search(t)) and bool(only.match(t))


# agent lines the caller never heard (voice/transcript.py NOT_PLAYED_STATES)
_NOT_PLAYED = frozenset({"undelivered", "cancelled"})


def last_agent_said_farewell(transcript: Any, language: str) -> bool:
    """Did the agent's reply to the latest caller turn contain a goodbye the
    caller heard?

    Lines that never played (failed or cancelled) are skipped: a goodbye the
    caller did not hear is not one the agent has said. The search stops at
    the previous caller line, so a goodbye from earlier in the call — before
    the caller asked something else — does not count.
    """
    entries = list(getattr(transcript, "entries", None) or [])
    if entries and _is_caller(entries[-1]):
        entries.pop()  # the caller's own goodbye, already logged
    for entry in reversed(entries):
        if _is_caller(entry):
            return False
        if not _speaker(entry).endswith("agent") or str(getattr(entry, "state", "") or "") in _NOT_PLAYED:
            continue
        return contains_farewell(str(getattr(entry, "text", "") or ""), language)
    return False


def _speaker(entry: Any) -> str:
    speaker = getattr(entry, "speaker", "")
    return str(getattr(speaker, "value", speaker)).lower()


def _is_caller(entry: Any) -> bool:
    return _speaker(entry).endswith("caller")


def estimate_speech_seconds(text: str) -> float:
    """Rough TTS play time for ``text`` (~150 wpm), capped at 12 s."""
    words = len(str(text or "").split())
    if not words:
        return 0.0
    return min(12.0, 0.35 + words * 0.4)


__all__ = [
    "END_CALL_TOKEN",
    "contains_farewell",
    "estimate_speech_seconds",
    "is_farewell",
    "last_agent_said_farewell",
    "parse_end_call_token",
]
