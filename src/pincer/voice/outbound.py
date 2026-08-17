"""
Outbound call initiator — agent tool to place phone calls on behalf of users.

Validates numbers, checks approval, calls Twilio REST API, and connects
the call to the voice gateway.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")

_daily_outbound_counts: dict[str, dict[str, int]] = {}


def _today_str() -> str:
    """Daily-limit day boundary in the configured voice timezone (not UTC)."""
    try:
        from pincer.config import get_settings
        from pincer.voice.localtime import voice_today_str

        return voice_today_str(get_settings())
    except Exception:
        return datetime.now(UTC).strftime("%Y-%m-%d")


def _check_daily_limit(user_id: str, max_daily: int) -> bool:
    """Check if user has exceeded daily outbound call limit."""
    today = _today_str()
    user_counts = _daily_outbound_counts.setdefault(user_id, {})
    count = user_counts.get(today, 0)
    return count < max_daily


def _increment_daily_count(user_id: str) -> None:
    today = _today_str()
    user_counts = _daily_outbound_counts.setdefault(user_id, {})
    user_counts[today] = user_counts.get(today, 0) + 1


def validate_e164(number: str) -> str | None:
    """Validate and normalize a phone number to E.164 format."""
    cleaned = re.sub(r"[\s\-\(\)]", "", number)
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if E164_PATTERN.match(cleaned):
        return cleaned
    return None


async def make_phone_call(
    target_number: str,
    purpose: str,
    instructions: str = "",
    max_duration: int = 300,
    language: str = "",
    context: dict | None = None,
) -> str:
    """Place a phone call to a number on behalf of the user.

    target_number: Phone number in E.164 format (e.g. +14155551234)
    purpose: What the call is about (e.g. 'Reschedule dentist appointment')
    instructions: Specific instructions for the agent during the call
    max_duration: Maximum call duration in seconds (default 300)
    language: Call language ('en', 'de', or 'uk'); empty = default language setting
    """
    from pincer.config import get_settings

    settings = get_settings()

    if not settings.voice_enabled:
        logger.info("make_phone_call aborted: voice_enabled=false")
        return "Error: Voice calling is not enabled. Set PINCER_VOICE_ENABLED=true."

    if not settings.voice_outbound_enabled:
        logger.info("make_phone_call aborted: voice_outbound_enabled=false")
        return "Error: Outbound calling is disabled. Set PINCER_VOICE_OUTBOUND_ENABLED=true."

    if not settings.voice_webhook_base_url or not settings.voice_webhook_base_url.strip().startswith("http"):
        logger.info("make_phone_call aborted: webhook URL missing or invalid")
        return (
            "Error: PINCER_VOICE_WEBHOOK_BASE_URL must be set to a public HTTPS URL for outbound calls. "
            "Use ngrok or a deployed URL."
        )

    validated = validate_e164(target_number)
    if not validated:
        logger.info("make_phone_call aborted: invalid E.164 format for %s", target_number)
        return f"Error: Invalid phone number format: {target_number}. Use E.164 format (e.g. +14155551234)."

    ctx = context or {}
    user_id = ctx.get("user_id", "unknown")

    if not _check_daily_limit(user_id, settings.voice_outbound_max_daily):
        logger.info("make_phone_call aborted: daily limit reached for user %s", user_id)
        return f"Error: Daily outbound call limit reached ({settings.voice_outbound_max_daily}). Try again tomorrow."

    try:
        from twilio.rest import Client

        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
        )

        base_url = settings.voice_webhook_base_url.strip().rstrip("/")
        status_url = f"{base_url}/api/apps/twilio/status"

        from pincer.voice.language import resolve_call_language
        from pincer.voice.twiml_builder import build_connect_twiml

        # Sprint 2: language is a parameter of the call
        call_language = resolve_call_language(settings, language)

        # Consent announcement plays as soon as the callee answers; the shared
        # builder (T4.1) picks engine, TTS provider, and voice from settings.
        twiml_str = build_connect_twiml(
            settings,
            direction="outbound",
            language=call_language,
            counterparty=validated,
        )

        call_kwargs: dict = {
            "to": validated,
            "from_": settings.twilio_phone_number,
            "twiml": twiml_str,
            "status_callback": status_url,
            "status_callback_event": ["ringing", "answered", "completed"],
            # TwiML execution errors play Pincer's own localized apology (and
            # land in our logs with ErrorCode) instead of Twilio's canned
            # "an application error has occurred".
            "fallback_url": f"{base_url}/api/apps/twilio/fallback",
            "fallback_method": "POST",
            "timeout": 30,
            "time_limit": min(max_duration, settings.voice_max_call_duration),
        }
        # T1.3: Answering Machine Detection — voicemail is detected and
        # reported to the user, never conversed with (see twiml_server /status).
        if getattr(settings, "voice_machine_detection", True):
            call_kwargs["machine_detection"] = "Enable"

        call = client.calls.create(**call_kwargs)

        _increment_daily_count(user_id)

        # T1.5: live status updates back to the initiating user (max 3/call)
        from pincer.voice.status_notify import notify_dialing, register_outbound_call

        register_outbound_call(
            call.sid,
            user_id=user_id,
            channel=ctx.get("channel", ""),
            purpose=purpose,
            target_number=validated,
            language=call_language,
        )
        await notify_dialing(call.sid)

        logger.info(
            "Outbound call placed: %s -> %s (purpose: %s)",
            call.sid,
            validated,
            purpose,
        )

        return (
            f"Call initiated successfully.\n"
            f"Call SID: {call.sid}\n"
            f"To: {validated}\n"
            f"Purpose: {purpose}\n"
            f"The call is now ringing. I'll update you when it connects."
        )

    except ImportError:
        logger.info("make_phone_call aborted: Twilio SDK not installed")
        return "Error: Twilio SDK not installed. Install with: uv pip install 'pincer-agent[voice]'"
    except Exception as e:
        err_msg = f"Error placing call: {e}"
        logger.warning("make_phone_call failed: %s", err_msg)
        logger.exception("Twilio exception details")
        return err_msg
