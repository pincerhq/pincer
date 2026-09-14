"""
Per-call language resolution and provider mappings (Sprint 2).

Language is a parameter of the call, not a global setting: `CallState.language`
carries it and every downstream component (prompts, STT, TTS, TwiML, consent)
reads from there. These helpers centralize validation and the language ->
provider-specific code/voice/model mappings.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LANGUAGE = "en"
KNOWN_LANGUAGES = ("en", "de", "uk")

# Twilio ConversationRelay attributes
RELAY_LANGUAGE = {"en": "en-US", "de": "de-DE", "uk": "uk-UA"}
RELAY_VOICE = {
    "en": "Google.en-US-Neural2-F",
    "de": "Google.de-DE-Neural2-F",
    "uk": "Google.uk-UA-Wavenet-A",
}

# ElevenLabs defaults (Sprint 4): one multilingual low-latency model for every
# language, and a documented default voice ("Rachel") when nothing is configured.
ELEVENLABS_DEFAULT_MODEL = "eleven_flash_v2_5"
ELEVENLABS_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

CR_TTS_PROVIDERS = ("google", "amazon", "elevenlabs")

# Casing Twilio documents for the ttsProvider attribute
CR_PROVIDER_ATTR = {"google": "Google", "amazon": "Amazon", "elevenlabs": "ElevenLabs"}

# ConversationRelay's ElevenLabs voice format is `voiceId[-model]` where the
# model drops the `eleven_` prefix; flash_v2_5 is Twilio's default (no suffix).
CR_ELEVENLABS_MODEL_SUFFIX = {
    "eleven_flash_v2": "flash_v2",
    "eleven_turbo_v2_5": "turbo_v2_5",
    "eleven_turbo_v2": "turbo_v2",
}


def _normalize(language: str) -> str:
    return str(language or "").strip().lower()[:2]


def supported_languages(settings: Any) -> list[str]:
    """Languages accepted for the per-call parameter (defensive parse)."""
    raw = getattr(settings, "voice_supported_languages", "") or ""
    langs = [_normalize(part) for part in str(raw).split(",") if _normalize(part) in KNOWN_LANGUAGES]
    return langs or list(KNOWN_LANGUAGES)


def resolve_call_language(settings: Any, requested: str = "") -> str:
    """Resolve the language for a call: explicit request > default setting >
    legacy PINCER_VOICE_LANGUAGE prefix > 'en'. Unsupported values fall back."""
    supported = supported_languages(settings)

    candidate = _normalize(requested)
    if candidate in supported:
        return candidate

    candidate = _normalize(getattr(settings, "voice_default_language", "") or "")
    if candidate in supported:
        return candidate

    candidate = _normalize(getattr(settings, "voice_language", "") or "")
    if candidate in supported:
        return candidate

    return DEFAULT_LANGUAGE


def de_formality(settings: Any) -> str:
    """German register: 'sie' (default) or 'du'."""
    value = str(getattr(settings, "voice_de_formality", "") or "").strip().lower()
    return "du" if value == "du" else "sie"


def relay_language(language: str) -> str:
    return RELAY_LANGUAGE.get(_normalize(language), RELAY_LANGUAGE[DEFAULT_LANGUAGE])


def relay_voice(language: str) -> str:
    return RELAY_VOICE.get(_normalize(language), RELAY_VOICE[DEFAULT_LANGUAGE])


def stt_language(language: str) -> str:
    """Deepgram language code."""
    return _normalize(language) or DEFAULT_LANGUAGE


def _clean(value: Any) -> str:
    """Defensive: mocked/unset settings attributes must never leak into TwiML."""
    text = str(value or "").strip()
    return "" if text.startswith("<") else text


def elevenlabs_voice_for(settings: Any, language: str, override: str = "") -> str:
    """Configured ElevenLabs voice: per-call override > language-specific > global.

    Returns "" when no voice is configured (callers decide the default —
    see `voice_for` for the resolver both engines use).
    """
    if _clean(override):
        return _clean(override)
    lang = _normalize(language)
    per_language = _clean(getattr(settings, f"elevenlabs_voice_id_{lang}", ""))
    if per_language:
        return per_language
    return _clean(getattr(settings, "elevenlabs_voice_id", ""))


def voice_for(settings: Any, language: str, override: str = "") -> str:
    """The ElevenLabs voice both engines speak with for this call.

    Resolution order (T4.3): per-call override > language-specific setting >
    global setting > documented default (Rachel).
    """
    return elevenlabs_voice_for(settings, language, override) or ELEVENLABS_DEFAULT_VOICE_ID


def elevenlabs_model_for(settings: Any, language: str = "") -> str:
    """Configured ElevenLabs model; the default is multilingual, so there is
    no per-language model switch anymore (Sprint 4)."""
    model = _clean(getattr(settings, "elevenlabs_model", ""))
    if model.startswith("eleven"):
        return model
    return ELEVENLABS_DEFAULT_MODEL


def cr_tts_provider(settings: Any, language: str) -> str:
    """ConversationRelay TTS provider: explicit setting, else auto —
    elevenlabs when an ElevenLabs voice is configured, google otherwise."""
    configured = _clean(getattr(settings, "cr_tts_provider", "")).lower()
    if configured in CR_TTS_PROVIDERS:
        return configured
    return "elevenlabs" if elevenlabs_voice_for(settings, language) else "google"


def relay_voice_attr(settings: Any, language: str, provider: str = "") -> str:
    """Value for the ConversationRelay `voice` attribute.

    ElevenLabs: `voiceId[-model]` per Twilio's documented format (bare ID
    means Twilio's default model, flash_v2_5 — same as ours). Otherwise the
    per-language Google voice string.
    """
    provider = provider or cr_tts_provider(settings, language)
    if provider == "elevenlabs":
        voice = voice_for(settings, language)
        suffix = CR_ELEVENLABS_MODEL_SUFFIX.get(elevenlabs_model_for(settings, language))
        return f"{voice}-{suffix}" if suffix else voice
    return relay_voice(language)
