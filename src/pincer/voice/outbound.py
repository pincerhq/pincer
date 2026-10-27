"""
Outbound call initiator — agent tool to place phone calls on behalf of users.

Validates numbers, checks approval, calls Twilio REST API, and connects
the call to the voice gateway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pincer.voice.briefing import BriefingError, CallBriefing, log_briefing_bound
from pincer.voice.engine import CLAIMED_SID_KEY
from pincer.voice.threads import KIND_FOLLOWUP, KIND_ORIGIN, ThreadError, truncate
from pincer.voice.twilio_rest import DialOutcomeUnknown

logger = logging.getLogger(__name__)

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")

#: SGR/CSI sequences. `TwilioRestException.__str__` colours its output whenever
#: stderr is a tty, and its own source notes the hazard: "someone might catch
#: this error and try to display the message from it to an end user". We do
#: exactly that — the dashboard shows `detail` verbatim — so the codes are
#: stripped here rather than left to paint `[31m[49m` across the caller's UI.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


#: Twilio codes that all mean "the number you dialled *from* is not usable on
#: this account". Twilio names the number but not the knob that sets it, and the
#: owner reading the dashboard has no way to know it is an env var — so we say.
_FROM_NUMBER_CODES = frozenset({21210, 21212, 21606, 21659})

#: The rejecting account is in the request URI Twilio echoes back
#: (`/Accounts/AC…/Calls.json`). Naming it turns the most common cause of this
#: failure — credentials pointing at a different Twilio project than the one
#: that owns the number — from a guess into something you can read off.
_ACCOUNT_SID_RE = re.compile(r"/Accounts/(AC[0-9a-fA-F]{32})/")


def _from_number_hint(exc: Exception) -> str:
    match = _ACCOUNT_SID_RE.search(str(getattr(exc, "uri", "") or ""))
    account = f" account {match.group(1)}" if match else " the account in PINCER_TWILIO_ACCOUNT_SID"
    return (
        f"Set PINCER_TWILIO_PHONE_NUMBER to a number{account} has purchased or "
        "verified, or point PINCER_TWILIO_ACCOUNT_SID/AUTH_TOKEN at the project "
        "that owns the number you want to call from."
    )


def twilio_error_text(exc: Exception) -> str:
    """A one-line, human-readable rendering of a failed Twilio request.

    `str(TwilioRestException)` is a multi-line, ANSI-coloured block built for a
    terminal. Every field it prints is available as an attribute, so we format
    the useful ones ourselves and get a message that is safe to log, to return
    over HTTP and to show to an owner.
    """
    msg = getattr(exc, "msg", None)
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if msg:
        parts = [str(msg).strip()]
        if code in _FROM_NUMBER_CODES:
            parts.append(_from_number_hint(exc))
        if code:
            parts.append(f"(Twilio error {code}: https://www.twilio.com/docs/errors/{code})")
        elif status:
            parts.append(f"(HTTP {status})")
        return " ".join(parts)
    return _ANSI_RE.sub("", str(exc)).strip()


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


# Twilio call statuses under which a dial is still a live call worth briefing.
# Anything else found by the post-timeout lookup is over (completed, busy,
# no-answer, failed, canceled) and must not be promoted: the status callback
# that would have cleaned it up has already come and gone.
LIVE_CALL_STATUSES = frozenset({"queued", "initiated", "ringing", "in-progress"})


class _DialEnded(Exception):
    """A timed-out dial that Twilio placed, but which ended before it was confirmed."""

    def __init__(self, call_sid: str, status: str) -> None:
        super().__init__(f"{call_sid} ended: {status}")
        self.call_sid = call_sid
        self.status = status


async def _recover_unconfirmed_dial(
    dialer: Any,
    settings: Any,
    to_number: str,
    dialed_at: datetime,
    *,
    trace_key: str,
    error: str,
) -> str:
    """The CallSid of an unconfirmed dial that is still live, or "" when unknown.

    Looks the call up at Twilio. Raises _DialEnded when the call was placed
    but is already over.
    """
    from pincer.observability.failure_codes import NOT_CONNECTED_STATUS, FailureCode
    from pincer.voice.telemetry import hooks as telemetry
    from pincer.voice.telemetry.outcomes import STATUS_FAILED

    call = None
    try:
        call = await dialer.find_call(to=to_number, from_=settings.twilio_phone_number, since=dialed_at)
    except Exception:
        logger.exception("Lookup of an unconfirmed dial to %s failed", to_number)
    if call is None:
        await telemetry.dial_unknown(trace_key, error=error)
        logger.warning("Outbound dial to %s unconfirmed (%s) with no call found; outcome unknown", to_number, error)
        return ""
    call_sid = str(call.sid)
    status = str(getattr(call, "status", "") or "").lower()
    if status in LIVE_CALL_STATUSES:
        logger.warning("Outbound dial to %s unconfirmed but Twilio placed it as %s (%s)", to_number, call_sid, status)
        return call_sid
    logger.warning("Outbound dial to %s unconfirmed; placed as %s but already ended (%s)", to_number, call_sid, status)
    telemetry.dial_accepted(trace_key, call_sid)
    # Twilio's status is not ours: `completed` here means a call nobody was
    # briefed for, never a success. busy / no-answer / failed / canceled have
    # their codes; anything else is unknown.
    await telemetry.call_ended(
        call_sid,
        status=STATUS_FAILED,
        failure_code=str(NOT_CONNECTED_STATUS.get(status, FailureCode.UNKNOWN)),
        termination_reason="ended_before_confirmed",
    )
    raise _DialEnded(call_sid, status or "unknown")


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
    the reason and goal the agent opens the call with, and it is REQUIRED.
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
        # reported to the user, never conversed with (see twiml_server /amd).
        #
        # AsyncAmd is required, not a tuning knob. Twilio's default is
        # synchronous AMD, which *blocks the call* until detection finishes:
        # the callee picks up and hears pure silence — no welcomeGreeting, no
        # ConversationRelay — for as long as detection takes, up to
        # machine_detection_timeout (30s default). Async AMD connects the
        # callee immediately and posts the verdict to the callback below,
        # which is the only thing we ever used it for.
        if getattr(settings, "voice_machine_detection", True):
            call_kwargs["machine_detection"] = "Enable"
            call_kwargs["async_amd"] = "true"
            call_kwargs["async_amd_status_callback"] = f"{base_url}/api/apps/twilio/amd"
            call_kwargs["async_amd_status_callback_method"] = "POST"

        # §2 Fix A: the briefed state exists BEFORE the dial, so a relay
        # `setup` that beats the REST response finds a briefed call instead of
        # inventing an unbriefed inbound one.
        engine = _voice_engine()
        pre_state = None
        if engine is not None:
            pre_state = await engine.register_pending_outbound(
                briefing, validated, language=call_language, instructions=briefing.instructions
            )

        # Telephony telemetry starts BEFORE the dial: an outbound call that
        # Twilio rejects has no CallSid, and without a pre-dial trace it would
        # be invisible in the dashboard exactly when someone asks why the call
        # never happened. The temporary key is re-bound to the real SID below.
        from pincer.voice.telemetry import hooks as telemetry

        # Unique per ATTEMPT, not per destination. Keyed on the number, two
        # calls to the same callee collided: the second `start_call` found the
        # first's still-live pending entry and returned its tracer, so the
        # second call's pre-dial events landed on the first call's row and its
        # own `dial_accepted` re-key then found nothing to re-key.
        trace_key = pre_state.call_sid if pre_state is not None else f"pending-{uuid4().hex}"
        telemetry.dial_requested(
            trace_key,
            to_number=validated,
            engine=str(getattr(settings, "voice_engine", "") or ""),
            language=call_language,
        )

        # The engine and TwilioRest share create_call / find_call.
        dialer: Any
        if engine is not None:
            dialer = engine
        else:
            # No voice engine in this process (API-only deployment): a
            # one-off session, closed here so it never outlives its loop.
            from pincer.voice.twilio_rest import TwilioRest

            dialer = TwilioRest(settings)
        dialed_at = datetime.now(UTC)
        call_sid = ""
        try:
            try:
                call = await dialer.create_call(**call_kwargs)
                call_sid = str(call.sid)
            except asyncio.CancelledError:
                # The turn that called the tool was cancelled mid-POST. Twilio
                # may still place the call: an unknown outcome, counted like
                # one. The pending entry goes, or a later setup to this number
                # could claim its stale briefing. Shielded: a second cancel
                # must not skip the cooldown write.
                if engine is not None and pre_state is not None:
                    engine.discard_pending(pre_state)
                _increment_daily_count(user_id)
                await asyncio.shield(telemetry.dial_unknown(trace_key, error="CancelledError: dial outcome unknown"))
                await asyncio.shield(
                    record_outbound_call(settings, validated, user_id=user_id, channel=ctx.get("channel", ""))
                )
                raise
            except DialOutcomeUnknown as unknown:
                # A timeout or a connection dropped mid-response is not a
                # rejection: Twilio may have accepted the POST and the phone
                # may already be ringing — or answered, in which case the
                # relay setup has claimed the pending state by callee number
                # (engine.claim_pending_outbound) and the SID is known here.
                claimed = str(pre_state.metadata.get(CLAIMED_SID_KEY) or "") if pre_state is not None else ""
                if claimed:
                    # Claimed, so placed — even if the call is already over,
                    # which a live lookup would mistake for "never bound".
                    call_sid = claimed
                    logger.warning(
                        "Outbound dial to %s unconfirmed (%s); the relay bound it as %s", validated, unknown, call_sid
                    )
                else:
                    call_sid = await _recover_unconfirmed_dial(
                        dialer,
                        settings,
                        validated,
                        dialed_at,
                        trace_key=trace_key,
                        error=str(unknown),
                    )
                if not call_sid:
                    # Outcome unknown, or the call already ended. It still
                    # counts toward the daily limit and the target cooldown,
                    # so a retry cannot ring the same person twice or slip
                    # past the cap. The pre-registration goes: nothing can
                    # bind to its temporary key, and a relay setup for an
                    # unbriefed outbound call is hung up.
                    if engine is not None and pre_state is not None:
                        engine.discard_pending(pre_state)
                    _increment_daily_count(user_id)
                    await record_outbound_call(settings, validated, user_id=user_id, channel=ctx.get("channel", ""))
                    return (
                        f"Error: Twilio did not confirm the call to {validated}, and it may still "
                        "be ringing. Do not call again right away; check the call log first."
                    )
            finally:
                if engine is None:
                    await dialer.aclose()
        except _DialEnded as ended:
            # Placed, but over before Twilio confirmed it (busy, declined,
            # failed...): a real attempt for the limit and the cooldown, but
            # no live call to brief, notify about, or thread.
            if engine is not None and pre_state is not None:
                engine.discard_pending(pre_state)
            _increment_daily_count(user_id)
            await record_outbound_call(
                settings, validated, user_id=user_id, channel=ctx.get("channel", ""), call_sid=ended.call_sid
            )
            return f"Error: the call to {validated} was placed ({ended.call_sid}) but already ended: {ended.status}."
        except Exception as e:
            await telemetry.dial_rejected(trace_key, error=f"{type(e).__name__}: {e}"[:160])
            if engine is not None and pre_state is not None:
                engine.discard_pending(pre_state)
            raise

        telemetry.dial_accepted(trace_key, call_sid)
        if engine is not None and pre_state is not None:
            await engine.promote_pending(pre_state, call_sid)
        # A dial the relay claimed can be over before the REST call returns:
        # answered, talked and hung up inside the request window. It happened,
        # so it is recorded and threaded, but nobody is told it is ringing.
        finished = engine is not None and engine.get_call_state(call_sid) is None
        log_briefing_bound(call_sid, briefing)

        _increment_daily_count(user_id)
        await record_outbound_call(
            settings,
            validated,
            user_id=user_id,
            channel=ctx.get("channel", ""),
            call_sid=call_sid,
        )

        # Sprint 13: the call joins (or starts) the thread for this matter
        # before any status update, so the thread id is already on the call row
        # when the engine builds the first turn's THREAD CONTEXT block.
        await _attach_thread(
            settings,
            call_sid,
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
            call_sid,
            user_id=user_id,
            channel=ctx.get("channel", ""),
            purpose=purpose,
            target_number=validated,
            language=call_language,
            target_name=str(target_name or "").strip(),
            instructions=str(instructions or "").strip(),
        )
        if finished:
            logger.info("Outbound call placed and already finished: %s -> %s", call_sid, validated)
            return f"Call placed and already finished.\nCall SID: {call_sid}\nTo: {validated}\nThe call report follows."
        await notify_dialing(call_sid)

        logger.info(
            "Outbound call placed: %s -> %s (purpose: %s)",
            call_sid,
            validated,
            purpose,
        )

        return (
            f"Call initiated successfully.\n"
            f"Call SID: {call_sid}\n"
            f"To: {validated}\n"
            f"Purpose: {purpose}\n"
            f"The call is now ringing. I'll update you when it connects."
        )

    except ImportError:
        logger.info("make_phone_call aborted: Twilio SDK not installed")
        return "Error: Twilio SDK not installed. Install with: uv pip install 'pincer-agent[voice]'"
    except Exception as e:
        err_msg = f"Error placing call: {twilio_error_text(e)}"
        logger.warning("make_phone_call failed: %s", err_msg)
        logger.exception("Twilio exception details")
        return err_msg
