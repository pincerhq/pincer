"""
ElevenLabs voice management — list, fetch, validate, sample synthesis (Sprint 4).

Backs `pincer voice list` / `pincer voice test`, the doctor voice checks, and
the startup validation that makes a bad voice ID fail loudly before the first
call instead of mid-call. Synchronous httpx on purpose: every caller (typer
CLI, doctor, startup wiring) is sync or can afford the one-off blocking call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
REQUEST_TIMEOUT_S = 10.0


class VoiceLookupError(Exception):
    """Network/transport failure talking to ElevenLabs — NOT proof a voice is bad."""


@dataclass
class VoiceInfo:
    voice_id: str
    name: str
    category: str  # premade | cloned | professional | generated
    languages: list[str] = field(default_factory=list)


def _headers(api_key: str) -> dict[str, str]:
    return {"xi-api-key": api_key}


def _parse_voice(data: dict[str, Any]) -> VoiceInfo:
    languages = [
        str(entry.get("language", "")) for entry in data.get("verified_languages") or [] if entry.get("language")
    ]
    return VoiceInfo(
        voice_id=str(data.get("voice_id", "")),
        name=str(data.get("name", "")),
        category=str(data.get("category", "")),
        languages=languages,
    )


def list_voices(api_key: str) -> list[VoiceInfo]:
    """All voices visible to the account (GET /v2/voices, paginated).

    This is how users find the ID of their own cloned voice.
    """
    import httpx

    voices: list[VoiceInfo] = []
    params: dict[str, Any] = {"page_size": 100}
    try:
        with httpx.Client(base_url=API_BASE, timeout=REQUEST_TIMEOUT_S) as client:
            while True:
                resp = client.get("/v2/voices", headers=_headers(api_key), params=params)
                resp.raise_for_status()
                body = resp.json()
                voices.extend(_parse_voice(v) for v in body.get("voices", []))
                token = body.get("next_page_token")
                if not token or not body.get("has_more"):
                    break
                params["next_page_token"] = token
    except httpx.HTTPStatusError as e:
        raise VoiceLookupError(f"ElevenLabs /v2/voices returned {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise VoiceLookupError(f"ElevenLabs unreachable: {e}") from e
    return voices


def get_voice(api_key: str, voice_id: str) -> VoiceInfo | None:
    """Fetch one voice by ID. None = the account has no such voice (any
    non-200 API answer); VoiceLookupError = could not ask (network)."""
    import httpx

    try:
        with httpx.Client(base_url=API_BASE, timeout=REQUEST_TIMEOUT_S) as client:
            resp = client.get(f"/v1/voices/{voice_id}", headers=_headers(api_key))
    except httpx.HTTPError as e:
        raise VoiceLookupError(f"ElevenLabs unreachable: {e}") from e
    if resp.status_code != 200:
        return None
    return _parse_voice(resp.json())


def synthesize_sample(
    api_key: str,
    voice_id: str,
    text: str,
    model: str = "eleven_flash_v2_5",
    output_format: str = "wav_16000",
) -> bytes:
    """Non-streaming synthesis for `pincer voice test` sample files.

    `ulaw_8000` gives telephony parity (some voices sound very different at
    8kHz mu-law than in the ElevenLabs web preview); `wav_16000` is playable.
    """
    import httpx

    try:
        with httpx.Client(base_url=API_BASE, timeout=60.0) as client:
            resp = client.post(
                f"/v1/text-to-speech/{voice_id}",
                headers=_headers(api_key),
                params={"output_format": output_format},
                json={"text": text, "model_id": model},
            )
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPStatusError as e:
        raise VoiceLookupError(
            f"ElevenLabs synthesis failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise VoiceLookupError(f"ElevenLabs unreachable: {e}") from e


def probe_voice(api_key: str, voice_id: str, model: str = "eleven_flash_v2_5") -> bool:
    """True if the voice can actually synthesize (minimal one-character request).

    Public-library and default voices synthesize by ID without appearing in
    the account's /v1/voices, so a metadata 404 alone doesn't condemn a voice.
    False = the API refused; VoiceLookupError = could not ask (network).
    """
    import httpx

    try:
        with httpx.Client(base_url=API_BASE, timeout=30.0) as client:
            resp = client.post(
                f"/v1/text-to-speech/{voice_id}",
                headers=_headers(api_key),
                params={"output_format": "ulaw_8000"},
                json={"text": ".", "model_id": model},
            )
    except httpx.HTTPError as e:
        raise VoiceLookupError(f"ElevenLabs unreachable: {e}") from e
    return resp.status_code == 200


def voice_usable(api_key: str, voice_id: str) -> bool:
    """Whether a voice ID is usable by this account: in My Voices, or a
    library/default voice that passes the synthesis probe."""
    if get_voice(api_key, voice_id) is not None:
        return True
    return probe_voice(api_key, voice_id)


def ulaw_to_wav(ulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """Wrap raw mu-law bytes in a WAV container (format tag 7) so the
    telephony-quality sample from `pincer voice test` is playable as-is."""
    import struct

    data_size = len(ulaw_bytes)
    # Non-PCM fmt chunk (18 bytes incl. cbSize=0): WAVE_FORMAT_MULAW, mono,
    # 8000 Hz, 1 byte/sample.
    fmt_chunk = b"fmt " + struct.pack("<IHHIIHHH", 18, 7, 1, sample_rate, sample_rate, 1, 8, 0)
    fact_chunk = b"fact" + struct.pack("<II", 4, data_size)
    data_chunk = b"data" + struct.pack("<I", data_size) + ulaw_bytes
    riff_size = 4 + len(fmt_chunk) + len(fact_chunk) + len(data_chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + fact_chunk + data_chunk


# ── Startup validation cache ──────────────────────────────────────────────────
# Resolved voice IDs are validated once at startup; a definitively-bad ID is
# remembered so the ConversationRelay TwiML builder can fall back to the
# Google voice (T4.5) and media_streams startup can fail loudly (T4.4).

_invalid_voice_ids: set[str] = set()
_verified_voice_ids: set[str] = set()


def is_voice_invalid(voice_id: str) -> bool:
    return voice_id in _invalid_voice_ids


def mark_voice_invalid(voice_id: str) -> None:
    """Runtime discovery that a voice is unusable (e.g. Twilio 64111 while
    synthesizing on ConversationRelay) — subsequent TwiML falls back to the
    Google voice until the configuration changes."""
    _invalid_voice_ids.add(voice_id)
    _verified_voice_ids.discard(voice_id)


def _reset_validation_cache_for_tests() -> None:
    _invalid_voice_ids.clear()
    _verified_voice_ids.clear()


def configured_voice_ids(settings: Any) -> set[str]:
    """The distinct ElevenLabs voice IDs this deployment can resolve to."""
    ids = {
        str(getattr(settings, name, "") or "").strip()
        for name in (
            "elevenlabs_voice_id",
            "elevenlabs_voice_id_en",
            "elevenlabs_voice_id_de",
            "elevenlabs_voice_id_uk",
        )
    }
    ids.discard("")
    return ids


def validate_configured_voices(settings: Any) -> dict[str, str]:
    """Validate every configured voice ID against the account, once.

    Returns {voice_id: problem} for IDs the account definitively does not
    have. Network failures are logged but do not condemn a voice (a flaky
    connection at startup must not disable a working configuration).
    """
    api_key = settings.elevenlabs_api_key.get_secret_value()
    ids = configured_voice_ids(settings)
    if not api_key or not ids:
        return {}

    problems: dict[str, str] = {}
    for voice_id in sorted(ids - _verified_voice_ids - _invalid_voice_ids):
        try:
            info = get_voice(api_key, voice_id)
            if info is not None:
                _verified_voice_ids.add(voice_id)
                logger.info("ElevenLabs voice verified: %s (%s, %s)", info.name, voice_id, info.category)
            elif probe_voice(api_key, voice_id):
                _verified_voice_ids.add(voice_id)
                logger.info(
                    "ElevenLabs voice %s usable via synthesis probe (library/default voice, not in My Voices)",
                    voice_id,
                )
            else:
                _invalid_voice_ids.add(voice_id)
                logger.error("ElevenLabs voice %s is not usable by this account", voice_id)
        except VoiceLookupError as e:
            logger.warning("Could not verify ElevenLabs voice %s: %s", voice_id, e)
            continue
    problems.update({vid: "not usable by this ElevenLabs account" for vid in _invalid_voice_ids & ids})
    return problems
