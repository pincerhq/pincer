"""
Recording consent and compliance — handles jurisdiction-aware consent
announcements, recording controls, and regulatory requirements.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)


class ConsentMode(StrEnum):
    ONE_PARTY = "one_party"
    TWO_PARTY = "two_party"
    NONE = "none"


TWO_PARTY_CONSENT_AREA_CODES = {
    "202",
    "203",
    "206",
    "209",
    "213",
    "310",
    "323",
    "341",
    "350",
    "408",
    "415",
    "424",
    "442",
    "510",
    "530",
    "559",
    "562",
    "619",
    "626",
    "628",
    "650",
    "657",
    "661",
    "669",
    "707",
    "714",
    "747",
    "760",
    "805",
    "818",
    "831",
    "858",
    "909",
    "916",
    "925",
    "949",
    "951",  # California
    "475",
    "860",  # Connecticut
    "302",  # Delaware
    "239",
    "305",
    "321",
    "352",
    "386",
    "407",
    "561",
    "727",
    "754",
    "772",
    "786",
    "813",
    "850",
    "863",
    "904",
    "941",
    "954",  # Florida
    "217",
    "224",
    "309",
    "312",
    "331",
    "618",
    "630",
    "708",
    "773",
    "779",
    "815",
    "847",
    "872",  # Illinois
    "301",
    "240",
    "410",
    "443",
    "667",  # Maryland
    "339",
    "351",
    "413",
    "508",
    "617",
    "774",
    "781",
    "857",
    "978",  # Massachusetts
    "406",  # Montana
    "603",  # New Hampshire
    "503",
    "541",
    "971",  # Oregon
    "215",
    "267",
    "272",
    "412",
    "445",
    "484",
    "570",
    "610",
    "717",
    "724",
    "814",
    "835",
    "878",  # Pennsylvania
    "253",
    "360",
    "425",
    "509",
    "564",  # Washington
}

CONSENT_ANNOUNCEMENT_EN = "This call may be recorded for quality purposes."

CONSENT_ANNOUNCEMENT_TWO_PARTY_EN = "This call may be recorded. By continuing this call, you consent to recording."

CONSENT_ANNOUNCEMENT_DE = "Dieser Anruf kann zu Qualitätszwecken aufgezeichnet werden."

CONSENT_ANNOUNCEMENT_TWO_PARTY_DE = (
    "Dieser Anruf wird möglicherweise aufgezeichnet. Wenn Sie das Gespräch fortsetzen, stimmen Sie der Aufzeichnung zu."
)

CONSENT_ANNOUNCEMENT_UK = "Цей дзвінок може записуватися для контролю якості."

CONSENT_ANNOUNCEMENT_TWO_PARTY_UK = "Цей дзвінок може записуватися. Продовжуючи розмову, ви даєте згоду на запис."

# Non-recording case: disclosing that an AI assistant is on the line is best
# practice (and increasingly required, e.g. EU AI Act Art. 50) even when no
# recording takes place.
AI_DISCLOSURE_EN = "Please note: you are speaking with an automated AI assistant."

AI_DISCLOSURE_DE = "Bitte beachten Sie: Sie sprechen mit einem automatischen KI-Assistenten."

AI_DISCLOSURE_UK = "Зверніть увагу: ви розмовляєте з автоматичним ШІ-асистентом."

OUTBOUND_RECORDING_DISCLOSURE = "I should let you know that this call may be recorded."

# Jurisdictions where German-language announcements are appropriate by default
GERMAN_SPEAKING_JURISDICTIONS = {"DE", "AT", "CH"}


@dataclass
class ConsentResult:
    consent_given: bool
    mode: ConsentMode
    announcement_played: bool
    jurisdiction: str = ""


def detect_jurisdiction(phone_number: str) -> str:
    """Detect jurisdiction from phone number for consent rules."""
    cleaned = re.sub(r"[\s\-\(\)\+]", "", phone_number)

    if cleaned.startswith("1") and len(cleaned) >= 11:
        area_code = cleaned[1:4]
        if area_code in TWO_PARTY_CONSENT_AREA_CODES:
            return "US-two-party"
        return "US-one-party"

    if cleaned.startswith("49"):
        return "DE"

    if cleaned.startswith("43"):
        return "AT"

    if cleaned.startswith("41"):
        return "CH"

    if cleaned.startswith("44"):
        return "UK"

    return "unknown"


def get_consent_mode(settings: Settings, caller_number: str) -> ConsentMode:
    """Determine the consent mode based on settings and caller jurisdiction."""
    configured = settings.voice_consent_mode.lower().strip()

    if configured == "none":
        return ConsentMode.NONE

    if configured == "two_party":
        return ConsentMode.TWO_PARTY

    jurisdiction = detect_jurisdiction(caller_number)

    if jurisdiction == "US-two-party":
        return ConsentMode.TWO_PARTY
    if jurisdiction == "DE":
        # §201 StGB: recording the non-public spoken word requires all-party consent
        return ConsentMode.TWO_PARTY
    if jurisdiction == "CH":
        # Art. 179ter StGB: all-party consent required
        return ConsentMode.TWO_PARTY

    return ConsentMode(configured) if configured in ConsentMode.__members__.values() else ConsentMode.ONE_PARTY


def resolve_consent_language(settings: Settings, caller_number: str = "") -> str:
    """Resolve the language for consent/disclosure announcements.

    Priority: explicit PINCER_VOICE_CONSENT_LANGUAGE > call language
    (PINCER_VOICE_LANGUAGE) > caller jurisdiction > 'en'.
    """
    configured = getattr(settings, "voice_consent_language", "") or ""
    if configured.strip():
        return configured.strip().lower()[:2]

    call_language = (getattr(settings, "voice_language", "") or "").strip().lower()
    if call_language and not call_language.startswith("en"):
        return call_language[:2]

    if detect_jurisdiction(caller_number) in GERMAN_SPEAKING_JURISDICTIONS:
        return "de"

    return "en"


def get_ai_disclosure(language: str = "en") -> str:
    """AI-assistant disclosure for calls without recording."""
    if language.lower().startswith("de"):
        return AI_DISCLOSURE_DE
    if language.lower().startswith("uk"):
        return AI_DISCLOSURE_UK
    return AI_DISCLOSURE_EN


def get_consent_announcement(
    mode: ConsentMode,
    caller_number: str = "",
    language: str = "",
    recording: bool = True,
) -> str | None:
    """Get the appropriate consent announcement text.

    When ``recording`` is False there is nothing to consent to, but the AI
    disclosure is still announced (unless mode is NONE). ``language`` overrides
    the jurisdiction-based language choice (use ``resolve_consent_language``).
    """
    if mode == ConsentMode.NONE:
        return None

    lang = language.lower()[:2] if language else ("de" if detect_jurisdiction(caller_number) == "DE" else "en")

    if not recording:
        return get_ai_disclosure(lang)

    if lang == "de":
        if mode == ConsentMode.TWO_PARTY:
            return CONSENT_ANNOUNCEMENT_TWO_PARTY_DE
        return CONSENT_ANNOUNCEMENT_DE

    if lang == "uk":
        if mode == ConsentMode.TWO_PARTY:
            return CONSENT_ANNOUNCEMENT_TWO_PARTY_UK
        return CONSENT_ANNOUNCEMENT_UK

    if mode == ConsentMode.TWO_PARTY:
        return CONSENT_ANNOUNCEMENT_TWO_PARTY_EN

    return CONSENT_ANNOUNCEMENT_EN


def build_intro_text(settings: Settings, language: str = "en") -> str:
    """Self-introduction spoken at call start, e.g.
    'This is Pincer, the AI assistant from 3days.ai and personal AI assistant of Jane Doe.'

    Configured via voice_assistant_name / voice_assistant_org / voice_assistant_owner;
    voice_intro_text overrides the whole sentence verbatim. Empty name = no introduction.
    """
    override = str(getattr(settings, "voice_intro_text", "") or "").strip()
    if override:
        return override

    name = str(getattr(settings, "voice_assistant_name", "") or "").strip()
    if not name:
        return ""
    org = str(getattr(settings, "voice_assistant_org", "") or "").strip()
    owner = str(getattr(settings, "voice_assistant_owner", "") or "").strip()

    if language.lower().startswith("de"):
        clauses = []
        if org:
            clauses.append(f"der KI-Assistent von {org}")
        if owner:
            clauses.append(
                f"persönlicher KI-Assistent von {owner}" if org else f"der persönliche KI-Assistent von {owner}"
            )
        text = f"Hier spricht {name}"
        if clauses:
            text += ", " + " und ".join(clauses)
        return text + "."

    if language.lower().startswith("uk"):
        clauses = []
        if org:
            clauses.append(f"ШІ-асистент від {org}")
        if owner:
            clauses.append(f"особистий ШІ-асистент {owner}")
        text = f"Це {name}"
        if clauses:
            text += ", " + " і ".join(clauses)
        return text + "."

    clauses = []
    if org:
        clauses.append(f"the AI assistant from {org}")
    if owner:
        clauses.append(f"personal AI assistant of {owner}" if org else f"the personal AI assistant of {owner}")
    text = f"This is {name}"
    if clauses:
        text += ", " + " and ".join(clauses)
    return text + "."


def build_call_opening(settings: Settings, remote_number: str, language: str = "") -> str:
    """Full spoken call opening: introduction, then consent/AI-disclosure.

    The introduction plays regardless of consent mode; the consent announcement
    follows when one applies. When recording is off and an introduction is
    configured, the separate AI-disclosure line is skipped — the introduction
    already discloses the AI assistant. ``language`` (per-call, Sprint 2)
    overrides the settings/jurisdiction-based language when given.
    """
    language = language.strip().lower()[:2] if language else resolve_consent_language(settings, remote_number)
    intro = build_intro_text(settings, language)

    mode = get_consent_mode(settings, remote_number)
    recording = bool(getattr(settings, "voice_recording_enabled", False))
    announcement = get_consent_announcement(mode, remote_number, language=language, recording=recording)
    if announcement and not recording and intro:
        announcement = None

    return " ".join(part for part in (intro, announcement) if part)


def build_consent_say_twiml(settings: Settings, remote_number: str, language: str = "") -> str:
    """TwiML ``<Say>`` for the call opening (introduction + consent/disclosure).

    Played before ``<Connect>`` so it precedes the conversation. Returns ''
    when nothing applies (no introduction configured and consent mode ``none``).
    ``language`` (the per-call language, Sprint 2) overrides the
    settings/jurisdiction-based announcement language when given.
    """
    from xml.sax.saxutils import escape

    text = build_call_opening(settings, remote_number, language=language)
    if not text:
        return ""
    resolved = language.strip().lower()[:2] if language else resolve_consent_language(settings, remote_number)
    say_language = {"de": "de-DE", "uk": "uk-UA"}.get(resolved, "en-US")
    return f'<Say language="{say_language}">{escape(text)}</Say>'


def should_record(settings: Settings, consent_given: bool) -> bool:
    """Determine if the call should be recorded based on settings and consent."""
    if not settings.voice_recording_enabled:
        return False
    if get_consent_mode(settings, "") == ConsentMode.NONE:
        return True
    return consent_given


class ComplianceChecker:
    """Validates compliance requirements for voice calls."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check_inbound_call(self, caller_number: str) -> ConsentResult:
        mode = get_consent_mode(self._settings, caller_number)
        announcement = get_consent_announcement(
            mode,
            caller_number,
            language=resolve_consent_language(self._settings, caller_number),
            recording=bool(getattr(self._settings, "voice_recording_enabled", True)),
        )
        jurisdiction = detect_jurisdiction(caller_number)

        return ConsentResult(
            consent_given=mode == ConsentMode.NONE,
            mode=mode,
            announcement_played=announcement is not None,
            jurisdiction=jurisdiction,
        )

    def check_outbound_call(self, target_number: str) -> ConsentResult:
        mode = get_consent_mode(self._settings, target_number)
        jurisdiction = detect_jurisdiction(target_number)

        return ConsentResult(
            consent_given=False,
            mode=mode,
            announcement_played=False,
            jurisdiction=jurisdiction,
        )
