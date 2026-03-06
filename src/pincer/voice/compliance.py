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
    "202", "203", "206", "209", "213", "310", "323", "341", "350", "408",
    "415", "424", "442", "510", "530", "559", "562", "619", "626", "628",
    "650", "657", "661", "669", "707", "714", "747", "760", "805", "818",
    "831", "858", "909", "916", "925", "949", "951",  # California
    "475", "860",  # Connecticut
    "302",  # Delaware
    "239", "305", "321", "352", "386", "407", "561", "727", "754", "772",
    "786", "813", "850", "863", "904", "941", "954",  # Florida
    "217", "224", "309", "312", "331", "618", "630", "708", "773", "779",
    "815", "847", "872",  # Illinois
    "301", "240", "410", "443", "667",  # Maryland
    "339", "351", "413", "508", "617", "774", "781", "857", "978",  # Massachusetts
    "406",  # Montana
    "603",  # New Hampshire
    "503", "541", "971",  # Oregon
    "215", "267", "272", "412", "445", "484", "570", "610", "717", "724",
    "814", "835", "878",  # Pennsylvania
    "253", "360", "425", "509", "564",  # Washington
}

CONSENT_ANNOUNCEMENT_EN = (
    "This call may be recorded for quality purposes."
)

CONSENT_ANNOUNCEMENT_TWO_PARTY_EN = (
    "This call may be recorded. By continuing this call, you consent to recording."
)

CONSENT_ANNOUNCEMENT_DE = (
    "Dieser Anruf kann zu Qualitätszwecken aufgezeichnet werden."
)

OUTBOUND_RECORDING_DISCLOSURE = (
    "I should let you know that this call may be recorded."
)


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
        return ConsentMode.TWO_PARTY

    return ConsentMode(configured) if configured in ConsentMode.__members__.values() else ConsentMode.ONE_PARTY


def get_consent_announcement(
    mode: ConsentMode, caller_number: str = "",
) -> str | None:
    """Get the appropriate consent announcement text."""
    if mode == ConsentMode.NONE:
        return None

    jurisdiction = detect_jurisdiction(caller_number)

    if jurisdiction == "DE":
        return CONSENT_ANNOUNCEMENT_DE

    if mode == ConsentMode.TWO_PARTY:
        return CONSENT_ANNOUNCEMENT_TWO_PARTY_EN

    return CONSENT_ANNOUNCEMENT_EN


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
        announcement = get_consent_announcement(mode, caller_number)
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
