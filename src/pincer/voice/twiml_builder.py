"""
Shared TwiML builder for voice calls (Sprint 4, T4.1).

One function builds the <Connect> TwiML for both engines and both directions,
so a voice/provider change happens in exactly one place. ConversationRelay
TTS is settings-driven: with an ElevenLabs voice configured (or
PINCER_CR_TTS_PROVIDER=elevenlabs) the relay speaks with that voice; a voice
ID that failed startup validation falls back to the Google voice with a
WARNING — the call still happens (T4.5).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>'

# Placeholder path segment for outbound Media Streams TwiML built before the
# call (and its SID) exists; the WebSocket start event carries the real SID.
CALL_SID_PLACEHOLDER = "{CallSid}"


def _extract_host(base_url: str) -> str:
    host = base_url
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host.rstrip("/")


def build_connect_twiml(
    settings: Any,
    call_sid: str = "",
    direction: str = "inbound",
    language: str = "",
    counterparty: str = "",
) -> str:
    """The full <Response> TwiML that connects a call to the agent.

    direction: "inbound" (webhook answer) or "outbound" (REST `twiml=` param).
    counterparty: the other party's number, used by the consent announcement.
    """
    from pincer.voice.language import (
        CR_PROVIDER_ATTR,
        cr_tts_provider,
        relay_language,
        relay_voice_attr,
        resolve_call_language,
        voice_for,
    )
    from pincer.voice.voices import is_voice_invalid

    lang = resolve_call_language(settings, language)
    base_url = str(settings.voice_webhook_base_url or "").strip().rstrip("/")
    engine_type = str(settings.voice_engine or "").lower().strip()
    inbound = direction != "outbound"

    parts = [XML_DECL, "<Response>"]

    if engine_type == "media_streams":
        from pincer.voice.compliance import build_consent_say_twiml

        # Consent / AI-disclosure announcement must precede the conversation;
        # media_streams has no native greeting, so it plays as a <Say>.
        parts.append(build_consent_say_twiml(settings, counterparty, language=lang))
        if inbound:
            connect_lines = {
                "en": "Please wait while I connect you to your assistant.",
                "de": "Einen Moment bitte, ich verbinde Sie mit Ihrem Assistenten.",
                "uk": "Зачекайте, будь ласка, з'єдную вас із вашим асистентом.",
            }
            connect_line = connect_lines.get(lang, connect_lines["en"])
            parts.append(f'<Say language="{relay_language(lang)}">{connect_line}</Say>')
        sid = call_sid or CALL_SID_PLACEHOLDER
        stream_url = f"wss://{_extract_host(base_url)}/api/apps/twilio/stream/{sid}"
        if inbound:
            status_url = f"{base_url}/api/apps/twilio/status"
            parts.append(f'<Connect><Stream url="{stream_url}" statusCallbackUrl="{status_url}" /></Connect>')
        else:
            parts.append(f'<Connect><Stream url="{stream_url}" /></Connect>')
    else:
        # ConversationRelay is WebSocket-only: the url attribute must be
        # wss:// (an https URL is rejected with Twilio error 64101 at
        # <Connect> time — "application error" right after the greeting).
        relay_url = f"wss://{_extract_host(base_url)}/api/apps/twilio/relay"
        provider = cr_tts_provider(settings, lang)
        voice_attr = f' voice="{relay_voice_attr(settings, lang, provider)}"'
        if provider == "elevenlabs" and is_voice_invalid(voice_for(settings, lang)):
            # Voice failed startup validation or live synthesis (Twilio 64111).
            # Omitting the attribute makes Twilio apply its documented
            # per-language default ElevenLabs voice — a guaranteed-valid
            # pairing in every supported language (better than Google, which
            # doesn't cover the full ConversationRelay language matrix).
            logger.warning(
                "Configured ElevenLabs voice %s is unusable — using Twilio's default %s ElevenLabs voice",
                voice_for(settings, lang),
                relay_language(lang),
            )
            voice_attr = ""

        # No robotic <Say> pre-roll on ConversationRelay: the call opening
        # (introduction + consent/AI-disclosure, Sprint 0) is passed as
        # welcomeGreeting and spoken in the relay's own configured voice.
        # Empty PINCER_VOICE_ASSISTANT_NAME + consent mode none = no greeting.
        from xml.sax.saxutils import escape

        from pincer.voice.compliance import build_call_opening

        opening = escape(build_call_opening(settings, counterparty, language=lang), {'"': "&quot;"})
        greeting_attr = f' welcomeGreeting="{opening}"' if opening else ""

        parts.append(
            f'<Connect><ConversationRelay url="{relay_url}"{voice_attr} '
            f'language="{relay_language(lang)}" '
            f'transcriptionProvider="Google" ttsProvider="{CR_PROVIDER_ATTR[provider]}"{greeting_attr} /></Connect>'
        )

    parts.append("</Response>")
    return "".join(parts)
