"""
Outbound call initiator — agent tool to place phone calls on behalf of users.

Validates numbers, checks approval, calls Twilio REST API, and connects
the call to the voice gateway.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from pincer.voice.briefing import BriefingError, CallBriefing, log_briefing_bound
from pincer.voice.threads import KIND_FOLLOWUP, KIND_ORIGIN, ThreadError, truncate

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


async def _audit_block(decision: Any, number: str, user_id: str, channel: str) -> None:
    """Record a gate-blocked dial attempt (best effort — never breaks the tool)."""
    try:
        from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger
        from pincer.voice.pii_guard import mask_phone_number

        audit = await get_audit_logger()
        await audit.log(
            AuditEntry(
                user_id=user_id or "unknown",
                action=AuditAction.VOICE_CALL_BLOCKED,
                tool="make_phone_call",
                input_summary=f"blocked: {decision.reason}",
                output_summary=mask_phone_number(number),
                approved=False,
                channel=channel or "voice",
                metadata={"reason": str(decision.reason), "retry_after_min": decision.retry_after_min},
            )
        )
    except Exception:  # pragma: no cover — auditing must not break the gate
        logger.debug("Audit logging of blocked call failed", exc_info=True)


def _voice_engine() -> Any:
    """The live engine, or None when voice is not running in this process
    (API-only deployments, tests). Pre-registration is best effort: without an
    engine there is no relay socket to race with."""
    try:
        from pincer.voice.twiml_server import get_engine

        return get_engine()
    except Exception:  # pragma: no cover — voice extra not installed
        return None


async def _validate_thread(settings: Any, thread_id: str) -> None:
    """Sprint 13 §4.2: a supplied thread must exist and must not be closed —
    checked BEFORE dialling, so a rejected follow-up never costs a phone call."""
    from pincer.voice.threads import get_thread_manager

    thread = await get_thread_manager(settings).get(thread_id)
    if thread is None:
        raise ThreadError(f"Thread {thread_id} does not exist.")
    if thread.status == "closed":
        raise ThreadError(
            f"Thread {thread_id} is closed and cannot take further calls. "
            "Start a new thread and reference this one in its subject."
        )


async def _attach_thread(
    settings: Any,
    call_sid: str,
    *,
    thread_id: str,
    kind: str,
    subject: str,
    number: str,
    contact_name: str,
    language: str,
) -> None:
    """Sprint 13 §2/§4: put the new call in its thread, creating the thread for
    a fresh task call. Best effort — thread bookkeeping must never turn a
    placed call into a reported failure."""
    from pincer.voice.threads import KIND_ORIGIN, ORIGIN_USER_TASK, get_thread_manager

    manager = get_thread_manager(settings)
    try:
        if not thread_id:
            thread = await manager.create(
                subject=subject,
                origin=ORIGIN_USER_TASK,
                primary_number=number,
                contact_name=contact_name,
                language=language,
            )
            thread_id, kind = thread.thread_id, KIND_ORIGIN
        await manager.attach(call_sid, thread_id, kind or KIND_ORIGIN)
    except Exception:
        logger.exception("Thread attach failed for call %s — the call itself is unaffected", call_sid)


async def make_phone_call(
    target_number: str,
    purpose: str,
    instructions: str = "",
    max_duration: int = 300,
    language: str = "",
    context: dict | None = None,
    target_name: str = "",
    thread_id: str = "",
    thread_kind: str = "",
    thread_subject: str = "",
    source: str = "",
) -> str:
    """Place a phone call to a number on behalf of the user.

    target_number: Phone number in E.164 format (e.g. +14155551234)
    purpose: What the call is about (e.g. 'Reschedule dentist appointment')
    instructions: Specific instructions for the agent during the call
    max_duration: Maximum call duration in seconds (default 300)
    language: Call language ('en', 'de', or 'uk'); empty = default language setting
    target_name: Who is being called (optional)
    thread_id: Continue an existing call thread (Sprint 13 §4.2); empty starts a new one
    thread_kind: Internal — how the call attaches (origin | retry | followup); callers
        that pass a thread_id but no kind get `followup`
    thread_subject: Internal — title for the thread created when thread_id is empty
    source: Internal — which surface asked for the call (dashboard | chat | api | scheduler)

    purpose and instructions are the agent's briefing for the call: purpose is
    the binding task the agent opens the call with, and it is REQUIRED.
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

    # The briefing is validated BEFORE the gate, the thread and the dial: a
    # call the agent cannot open with a task is not worth placing, and an
    # empty-purpose call has never been anything but a generic monologue.
    try:
        briefing = CallBriefing.create(
            purpose,
            target_name=target_name,
            language=language,
            source=source,
            instructions=instructions,
        )
    except BriefingError as e:
        logger.info("make_phone_call aborted: %s", e)
        return f"Error: {e}"

    thread_id = str(thread_id or "").strip()
    attach_kind = str(thread_kind or "").strip() or (KIND_FOLLOWUP if thread_id else KIND_ORIGIN)
    if thread_id:
        try:
            await _validate_thread(settings, thread_id)
        except ThreadError as e:
            logger.info("make_phone_call aborted: %s", e)
            return f"Error: {e}"

    ctx = context or {}
    user_id = ctx.get("user_id", "unknown")

    if not _check_daily_limit(user_id, settings.voice_outbound_max_daily):
        logger.info("make_phone_call aborted: per-user daily limit reached for user %s", user_id)
        return f"Error: Daily outbound call limit reached ({settings.voice_outbound_max_daily}). Try again tomorrow."

    # T8.3: the single server-side abuse gate — do-not-call list, quiet hours,
    # global daily cap, per-target cooldown. Every channel reaches Twilio
    # through this function, so there is no path around it, and automatic
    # retries consume the same budget (no retry storms).
    from pincer.voice.language import resolve_call_language
    from pincer.voice.safety_gates import check_outbound_allowed, record_outbound_call

    gate_language = resolve_call_language(settings, language)
    decision = await check_outbound_allowed(settings, validated, user_id=user_id, language=gate_language)
    if not decision.allowed:
        logger.warning(
            "make_phone_call blocked by outbound gate [%s] for user %s",
            decision.reason,
            user_id,
        )
        await _audit_block(decision, validated, user_id, ctx.get("channel", ""))
        return f"Error: {decision.message}"

    try:
        from twilio.rest import Client

        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
        )

        base_url = settings.voice_webhook_base_url.strip().rstrip("/")
        status_url = f"{base_url}/api/apps/twilio/status"

        from pincer.voice.twiml_builder import build_connect_twiml

        # Sprint 2: language is a parameter of the call
        call_language = gate_language

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

        # §2 Fix A: the briefed state exists BEFORE the dial, so a relay
        # `setup` that beats the REST response finds a briefed call instead of
        # inventing an unbriefed inbound one.
        engine = _voice_engine()
        pre_state = None
        if engine is not None:
            pre_state = await engine.register_pending_outbound(
                briefing, validated, language=call_language, instructions=briefing.instructions
            )

        try:
            call = client.calls.create(**call_kwargs)
        except Exception:
            if engine is not None and pre_state is not None:
                engine.discard_pending(pre_state)
            raise

        if engine is not None and pre_state is not None:
            await engine.promote_pending(pre_state, call.sid)
        log_briefing_bound(call.sid, briefing)

        _increment_daily_count(user_id)
        await record_outbound_call(
            settings,
            validated,
            user_id=user_id,
            channel=ctx.get("channel", ""),
            call_sid=call.sid,
        )

        # Sprint 13: the call joins (or starts) the thread for this matter
        # before any status update, so the thread id is already on the call row
        # when the engine builds the first turn's THREAD CONTEXT block.
        await _attach_thread(
            settings,
            call.sid,
            thread_id=thread_id,
            kind=attach_kind,
            subject=str(thread_subject or "").strip() or truncate(purpose, 120),
            number=validated,
            contact_name=str(target_name or "").strip(),
            language=call_language,
        )

        # T1.5: live status updates back to the initiating user (max 3/call)
        from pincer.voice.status_notify import notify_dialing, register_outbound_call

        register_outbound_call(
            call.sid,
            user_id=user_id,
            channel=ctx.get("channel", ""),
            purpose=purpose,
            target_number=validated,
            language=call_language,
            target_name=str(target_name or "").strip(),
            instructions=str(instructions or "").strip(),
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
