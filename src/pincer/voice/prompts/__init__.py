"""
Voice prompt i18n (Sprint 2).

`get_prompt(key, language)` returns the localized prompt/dict for a key, with
English as the fallback for missing keys or unsupported languages. German
supports a formality switch (Sie default; `formality="du"` applies the
informal overrides defined in `de.DU_OVERRIDES`).

The English constants are re-exported at package level for backward
compatibility with pre-i18n imports (`from pincer.voice.prompts import ...`).
"""

from __future__ import annotations

import logging
from typing import Any

from pincer.voice.prompts import de as _de
from pincer.voice.prompts import en as _en
from pincer.voice.prompts import uk as _uk

logger = logging.getLogger(__name__)

_MODULES = {"en": _en, "de": _de, "uk": _uk}

# (key, language) pairs already reported. The English fallback is a safety net,
# not a feature: on a non-English call it puts an English block in front of a
# model told to speak only that language, and an English sentence in front of
# the caller. Logged once per pair so a real gap is visible without flooding.
_FALLBACK_WARNED: set[tuple[str, str]] = set()


def get_prompt(key: str, language: str = "en", formality: str = "sie") -> Any:
    """Localized prompt for `key`; falls back to English for missing keys."""
    lang = str(language or "en").strip().lower()[:2]
    module = _MODULES.get(lang, _en)

    if module is _de and formality == "du":
        override = _de.DU_OVERRIDES.get(key)
        if override is not None:
            return override

    value = getattr(module, key, None)
    if value is None:
        if module is not _en and (key, lang) not in _FALLBACK_WARNED:
            _FALLBACK_WARNED.add((key, lang))
            logger.warning(
                "Voice prompt %r missing for language %r — falling back to English, "
                "which the caller will hear or the model will be steered by",
                key,
                lang,
            )
        value = getattr(_en, key, None)
    return value


def get_filler_phrases(language: str = "en") -> list[str]:
    phrases = get_prompt("FILLER_PHRASES", language)
    return list(phrases) if phrases else list(_en.FILLER_PHRASES)


# ── Backward-compatible English exports ───────────────────
VOICE_SYSTEM_PROMPT = _en.VOICE_SYSTEM_PROMPT
VOICE_GREETING_INBOUND = _en.VOICE_GREETING_INBOUND
VOICE_GREETING_OUTBOUND = _en.VOICE_GREETING_OUTBOUND
VOICE_VERIFY_PROMPT = _en.VOICE_VERIFY_PROMPT
VOICE_ERROR_PROMPT = _en.VOICE_ERROR_PROMPT
VOICE_ENDING_PROMPT = _en.VOICE_ENDING_PROMPT
IVR_NAVIGATION_PROMPT = _en.IVR_NAVIGATION_PROMPT
FILLER_PHRASES = _en.FILLER_PHRASES
LOW_CONFIDENCE_REPLY = _en.LOW_CONFIDENCE_REPLY

__all__ = [
    "FILLER_PHRASES",
    "IVR_NAVIGATION_PROMPT",
    "LOW_CONFIDENCE_REPLY",
    "VOICE_ENDING_PROMPT",
    "VOICE_ERROR_PROMPT",
    "VOICE_GREETING_INBOUND",
    "VOICE_GREETING_OUTBOUND",
    "VOICE_SYSTEM_PROMPT",
    "VOICE_VERIFY_PROMPT",
    "get_filler_phrases",
    "get_prompt",
]
