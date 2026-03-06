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


def _check_daily_limit(user_id: str, max_daily: int) -> bool:
    """Check if user has exceeded daily outbound call limit."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    user_counts = _daily_outbound_counts.setdefault(user_id, {})
    count = user_counts.get(today, 0)
    return count < max_daily


def _increment_daily_count(user_id: str) -> None:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
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
    context: dict | None = None,
) -> str:
    """Place a phone call to a number on behalf of the user.

    target_number: Phone number in E.164 format (e.g. +14155551234)
    purpose: What the call is about (e.g. 'Reschedule dentist appointment')
    instructions: Specific instructions for the agent during the call
    max_duration: Maximum call duration in seconds (default 300)
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
        return (
            f"Error: Daily outbound call limit reached ({settings.voice_outbound_max_daily}). "
            "Try again tomorrow."
        )

    try:
        from twilio.rest import Client

        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
        )

        base_url = settings.voice_webhook_base_url.strip().rstrip("/")
        status_url = f"{base_url}/voice/status"

        engine_type = settings.voice_engine.lower().strip()
        if engine_type == "media_streams":
            host = base_url
            for prefix in ("https://", "http://"):
                if host.startswith(prefix):
                    host = host[len(prefix):]
                    break
            host = host.rstrip("/")
            stream_url = f"wss://{host}/voice/stream/{{CallSid}}"
            twiml_str = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f'<Connect><Stream url="{stream_url}" /></Connect>'
                "</Response>"
            )
        else:
            relay_url = f"{base_url}/voice/relay-webhook"
            twiml_str = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f'<Connect><ConversationRelay url="{relay_url}" '
                f'voice="Google.en-US-Neural2-F" language="{settings.voice_language}" '
                'transcriptionProvider="google" ttsProvider="google" /></Connect>'
                "</Response>"
            )

        call = client.calls.create(
            to=validated,
            from_=settings.twilio_phone_number,
            twiml=twiml_str,
            status_callback=status_url,
            status_callback_event=["ringing", "answered", "completed"],
            timeout=30,
            time_limit=min(max_duration, settings.voice_max_call_duration),
        )

        _increment_daily_count(user_id)

        logger.info(
            "Outbound call placed: %s -> %s (purpose: %s)",
            call.sid, validated, purpose,
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
