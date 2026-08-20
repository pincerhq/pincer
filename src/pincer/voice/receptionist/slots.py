"""
TAKE_MESSAGE slot filling helpers (Sprint 12 §8.2) — deterministic.

The receptionist asks the four questions in a fixed order, one per turn, and
validates every answer without an LLM: names are spelled back when the
recognizer was unsure or the name is uncommon; dictated numbers are read back
grouped in pairs in digit words; the free-text matter is capped and
summarized back; urgency is asked only when the matter sounds time-critical.
Everything here is pure so the thresholds and read-backs are unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SPELLBACK_CONFIDENCE_THRESHOLD = 0.85
MAX_SLOT_ATTEMPTS = 2
MATTER_MAX_CHARS = 400  # ≈ 60s of speech
MATTER_SUMMARY_CHARS = 220

# A deliberately common-names list (de/en): names OUTSIDE it trigger the
# spell-back, names inside it (with a confident recognizer) are accepted as
# heard. Surnames are what callers usually give on a business line.
COMMON_NAMES: frozenset[str] = frozenset(
    {
        "müller",
        "mueller",
        "schmidt",
        "schneider",
        "fischer",
        "weber",
        "meyer",
        "meier",
        "wagner",
        "becker",
        "schulz",
        "hoffmann",
        "koch",
        "richter",
        "klein",
        "wolf",
        "schröder",
        "neumann",
        "schwarz",
        "braun",
        "zimmermann",
        "krüger",
        "hartmann",
        "lange",
        "werner",
        "krause",
        "lehmann",
        "köhler",
        "herrmann",
        "könig",
        "smith",
        "johnson",
        "williams",
        "brown",
        "jones",
        "miller",
        "davis",
        "wilson",
        "taylor",
        "anderson",
        "thomas",
        "moore",
        "martin",
        "jackson",
        "white",
        "harris",
        "clark",
        "lewis",
        "walker",
        "hall",
        "young",
        "king",
        "wright",
        "scott",
        "green",
        "baker",
        "adams",
        "nelson",
        "hill",
        "campbell",
        "mitchell",
        "roberts",
        "carter",
        "phillips",
        "evans",
        "turner",
        "parker",
        "collins",
        "edwards",
        "stewart",
        "morris",
        "murphy",
        "cook",
        "rogers",
        "morgan",
        "cooper",
        "peterson",
        "reed",
        "bailey",
        "bell",
        "kelly",
        "howard",
        "ward",
        "cox",
        "richardson",
        "wood",
        "watson",
        "brooks",
        "bennett",
        "gray",
        "james",
        "hughes",
        "price",
        "sanders",
        "myers",
        "long",
        "ross",
        "foster",
    }
)

_NAME_PREFIXES = (
    r"mein name ist",
    r"ich heiße",
    r"ich heisse",
    r"hier ist",
    r"hier spricht",
    r"ich bin",
    r"am apparat ist",
    r"my name is",
    r"this is",
    r"it's",
    r"it is",
    r"i am",
    r"i'm",
    r"мене звати",
    r"це",
    r"я",
)
_NAME_STRIP_RE = re.compile(r"^(?:" + "|".join(_NAME_PREFIXES) + r")\s+", re.IGNORECASE)
_NAME_TAIL_RE = re.compile(r"\s*(?:,|\.|!|\?| und | and |$).*", re.IGNORECASE)
_HONORIFIC_RE = re.compile(r"^(?:herr|frau|mr\.?|mrs\.?|ms\.?|dr\.?|пан|пані)\s+", re.IGNORECASE)
_NAME_TRAILERS = frozenset({"speaking", "here", "hier", "apparat", "calling", "anrufend"})

DIGIT_WORDS: dict[str, dict[str, str]] = {
    "de": {
        "null": "0",
        "eins": "1",
        "ein": "1",
        "eine": "1",
        "zwei": "2",
        "zwo": "2",
        "drei": "3",
        "vier": "4",
        "fünf": "5",
        "fuenf": "5",
        "sechs": "6",
        "sieben": "7",
        "acht": "8",
        "neun": "9",
    },
    "en": {
        "zero": "0",
        "oh": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    },
    "uk": {
        "нуль": "0",
        "один": "1",
        "одна": "1",
        "два": "2",
        "дві": "2",
        "три": "3",
        "чотири": "4",
        "п'ять": "5",
        "шість": "6",
        "сім": "7",
        "вісім": "8",
        "дев'ять": "9",
    },
}
_SPOKEN_DIGITS = {
    "de": ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"],
    "en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
    "uk": ["нуль", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"],
}

# "sounds time-critical" → the urgency question is asked (§8.2 row 4)
URGENT_KEYWORDS = (
    "dringend",
    "notfall",
    "sofort",
    "schmerz",
    "akut",
    "heute noch",
    "eilig",
    "blut",
    "unfall",
    "urgent",
    "emergency",
    "asap",
    "immediately",
    "pain",
    "today",
    "right away",
    "терміново",
    "негайно",
    "біль",
    "сьогодні",
)

_EMAIL_WORDS = {
    "at": "@",
    "ät": "@",
    "klammeraffe": "@",
    "punkt": ".",
    "dot": ".",
    "крапка": ".",
    "собака": "@",
    "minus": "-",
    "bindestrich": "-",
    "dash": "-",
    "hyphen": "-",
    "unterstrich": "_",
    "underscore": "_",
}


@dataclass
class MessageSlots:
    """The structured message (§11 inbound_messages row in the making)."""

    caller_name: str = ""
    caller_name_unverified: bool = False
    callback_number: str = ""
    callback_unverified: bool = False
    matter: str = ""
    urgent: bool = False
    email: str = ""
    name_attempts: int = 0
    number_attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caller_name": self.caller_name,
            "caller_name_unverified": self.caller_name_unverified,
            "callback_number": self.callback_number,
            "callback_unverified": self.callback_unverified,
            "matter": self.matter,
            "urgent": self.urgent,
            "email": self.email,
        }


# ── Names ────────────────────────────────────────────────────────────


def normalize_name(utterance: str) -> str:
    """'Ja, mein Name ist Müller, hallo' → 'Müller'. Keeps up to three words."""
    text = str(utterance or "").strip()
    text = re.sub(r"^(?:ja|yes|hallo|hello|hi|guten tag|also|ähm|äh|so)[,.!\s]+", "", text, flags=re.IGNORECASE)
    text = _NAME_STRIP_RE.sub("", text)
    while True:  # "Frau Dr. Weber" → "Weber"
        stripped = _HONORIFIC_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _NAME_TAIL_RE.sub("", text, count=1) if re.search(r"[,.!?]| und | and ", text) else text
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    while words and words[-1].lower().strip(".,!?") in _NAME_TRAILERS:
        words.pop()
    return " ".join(words[:3]).strip(" .,!?")


def is_common_name(name: str) -> bool:
    parts = [p.lower() for p in re.split(r"[\s-]+", str(name or "")) if p]
    return bool(parts) and parts[-1] in COMMON_NAMES


def needs_spellback(name: str, confidence: float | None = None) -> bool:
    """§8.2 row 1: spell back when the recognizer was unsure (< 0.85) OR the
    name is not in the common-names list. Unknown confidence counts as sure."""
    if not name:
        return True
    if confidence is not None and confidence < SPELLBACK_CONFIDENCE_THRESHOLD:
        return True
    return not is_common_name(name)


def spell_out(name: str, language: str = "de") -> str:
    """'Müller' → 'M-Ü-L-L-E-R'; multi-word names are spelled word by word."""
    words = [w for w in re.split(r"\s+", str(name or "").strip()) if w]
    sep = {"de": " dann ", "uk": " потім ", "en": " then "}.get(str(language)[:2], " then ")
    spelled = ["-".join(ch.upper() for ch in word if ch.isalpha() or ch == "-").replace("--", "-") for word in words]
    return sep.join(spelled)


# ── Numbers ──────────────────────────────────────────────────────────


def extract_digits(utterance: str, language: str = "de") -> str:
    """Digits dictated as words or numerals → '017212345678' (other words dropped)."""
    lang = str(language or "de")[:2]
    words_map = {**DIGIT_WORDS["en"], **DIGIT_WORDS["de"], **DIGIT_WORDS["uk"], **DIGIT_WORDS.get(lang, {})}
    out: list[str] = []
    for token in re.findall(r"\+|\d+|[^\W\d_]+(?:'[^\W\d_]+)?", str(utterance or "").lower()):
        if token == "+":
            if not out:
                out.append("+")
        elif token.isdigit():
            out.append(token)
        elif token in words_map:
            out.append(words_map[token])
        elif token in ("doppel", "double", "zwo"):  # "doppel drei" handled by repeating next
            out.append("§")
    digits = "".join(out)
    while "§" in digits:
        idx = digits.index("§")
        nxt = digits[idx + 1 : idx + 2]
        digits = digits[:idx] + (nxt if nxt.isdigit() else "") + digits[idx + 1 :]
    return digits


def normalize_callback_number(raw_digits: str, caller_id: str = "") -> str | None:
    """E.164-normalize a dictated number. A national '0…' number inherits the
    caller-ID country code (the best guess a phone line has); returns None
    when no valid E.164 form results."""
    from pincer.voice.outbound import validate_e164

    digits = str(raw_digits or "").strip()
    if not digits:
        return None
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and not digits.startswith("+"):
        country = re.match(r"^\+(\d{1,3})", str(caller_id or ""))
        if country:
            prefix = country.group(1)
            # Country-code width: 1 (+1 NANP, +7) else 2 (49, 43, 41, 44, 33, …), then 3
            widths = (1,) if prefix[0] in "17" else (2, 3)
            for width in widths:
                if len(prefix) >= width and validate_e164("+" + prefix[:width] + digits[1:]):
                    digits = "+" + prefix[:width] + digits[1:]
                    break
    return validate_e164(digits)


def readback_number(number: str, language: str = "de") -> str:
    """Read back a number grouped in pairs in digit words:
    '+4917212345678' → 'plus vier-neun, eins-sieben, zwei-eins, zwei-drei, vier-fünf, sechs-sieben, acht'."""
    lang = str(language or "de")[:2]
    words = _SPOKEN_DIGITS.get(lang, _SPOKEN_DIGITS["en"])
    plus = {"de": "plus ", "uk": "плюс ", "en": "plus "}.get(lang, "plus ")
    digits = re.sub(r"\D", "", str(number or ""))
    groups = [digits[i : i + 2] for i in range(0, len(digits), 2)]
    spoken = ", ".join("-".join(words[int(d)] for d in g) for g in groups)
    return (plus if str(number or "").startswith("+") else "") + spoken


def last4(number: str) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-4:]


def spoken_last4(number: str, language: str = "de") -> str:
    """'…1234' as digit words, grouped in pairs, for the caller-ID offer."""
    return readback_number(last4(number), language)


# ── Matter / urgency / email ─────────────────────────────────────────


def cap_matter(text: str) -> tuple[str, bool]:
    """(matter, needs_summary_confirmation). Long matters are trimmed and read back."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= MATTER_MAX_CHARS:
        return cleaned, False
    cut = cleaned[:MATTER_SUMMARY_CHARS]
    cut = cut[: cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:") + "…", True


def sounds_urgent(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(k in lowered for k in URGENT_KEYWORDS)


def extract_spelled_email(utterance: str) -> str:
    """'m punkt mueller at praxis punkt de' → 'm.mueller@praxis.de'. Letters
    spelled one by one are joined; returns '' when no '@' results."""
    tokens = re.findall(r"[^\W_]+|@|\.|-|_", str(utterance or "").lower())
    out: list[str] = []
    for tok in tokens:
        if tok in _EMAIL_WORDS:
            out.append(_EMAIL_WORDS[tok])
        elif tok in ("@", ".", "-", "_"):
            out.append(tok)
        elif re.fullmatch(r"[a-z0-9äöüß]+", tok):
            out.append(tok.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))
    email = "".join(out)
    return email if re.fullmatch(r"[a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,}", email) else ""


def spell_email(email: str, language: str = "de") -> str:
    lang = str(language or "de")[:2]
    words = {
        "de": {"@": " at ", ".": " Punkt ", "-": " Minus ", "_": " Unterstrich "},
        "en": {"@": " at ", ".": " dot ", "-": " dash ", "_": " underscore "},
        "uk": {"@": " собака ", ".": " крапка ", "-": " мінус ", "_": " підкреслення "},
    }.get(lang, {"@": " at ", ".": " dot ", "-": " dash ", "_": " underscore "})
    return "".join(words.get(ch, ch.upper() + " ") for ch in email).replace("  ", " ").strip()


__all__ = [
    "COMMON_NAMES",
    "MATTER_MAX_CHARS",
    "MAX_SLOT_ATTEMPTS",
    "SPELLBACK_CONFIDENCE_THRESHOLD",
    "URGENT_KEYWORDS",
    "MessageSlots",
    "cap_matter",
    "extract_digits",
    "extract_spelled_email",
    "is_common_name",
    "last4",
    "needs_spellback",
    "normalize_callback_number",
    "normalize_name",
    "readback_number",
    "sounds_urgent",
    "spell_email",
    "spell_out",
    "spoken_last4",
]
