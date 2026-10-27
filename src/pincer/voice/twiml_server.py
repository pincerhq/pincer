"""
TwiML server — FastAPI endpoints for Twilio voice webhooks.

Handles inbound call routing, status callbacks, ConversationRelay webhooks,
Media Streams WebSocket connections, and fallback error handling.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response
from starlette.websockets import WebSocketState

from pincer.voice.call_locks import one_call_update_at_a_time
from pincer.voice.twiml_builder import CALL_SID_PLACEHOLDER
from pincer.voice.webhook_auth import (
    WS_MONITOR_PATH,
    WS_RELAY_PATH,
    WS_STREAM_PATH,
    WebhookAuthError,
    audit_rejection,
    client_ip,
    verify_http_request,
    verify_ws_upgrade,
)

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

# How long a relay `setup` waits for an outbound call's state to be
# registered before the call is refused. The dial and the promotion are
# milliseconds apart; this is the margin, not a retry budget.
SETUP_STATE_WAIT_S = 3.0

# Twilio sends a media frame every 20 ms. Anything beyond this is a real gap in
# the inbound audio — network, provider buffering, or our own event loop being
# blocked. It does NOT attribute blame; it only says the audio stopped arriving.
MEDIA_GAP_THRESHOLD_MS = 120.0

# Set on `request.state` / `websocket.state` once the Twilio signature or our
# WS token was actually checked and passed. A request let through with auth
# off (or no auth token) never carries it.
VERIFIED_STATE_ATTR = "twilio_verified"

twilio_router = APIRouter(prefix="/api/apps/twilio", tags=["apps", "twilio"])
voice_router = APIRouter(prefix="/voice", tags=["voice"])

_engine: VoiceEngine | None = None
_settings: Settings | None = None


def init_voice_routes(engine: VoiceEngine, settings: Settings) -> None:
    """Wire up the voice engine and settings for route handlers."""
    global _engine, _settings  # noqa: PLW0603
    _engine = engine
    _settings = settings


def get_engine() -> VoiceEngine | None:
    """The live engine wired by init_voice_routes (None before startup)."""
    return _engine


async def _authenticate(request: Request) -> bytes | None:
    """Verify the Twilio signature and return the raw body, or None on rejection.

    T8.1: every /voice/* and /api/apps/twilio/* HTTP route runs through this —
    the routes are unauthenticated by any other means, and Starlette replays a
    body that has already been read, so `await request.form()` in the handler
    still works afterwards.
    """
    body = await request.body()
    try:
        verified = await verify_http_request(request, body, _settings)
    except WebhookAuthError as e:
        await audit_rejection(f"twilio_webhook:{request.url.path}", client_ip(request), e.reason)
        return None
    _mark_verified(request, verified)
    return body


def _mark_verified(conn: Request | WebSocket, verified: bool) -> None:
    with contextlib.suppress(AttributeError):
        setattr(conn.state, VERIFIED_STATE_ATTR, verified)


def _was_verified(conn: Any) -> bool:
    """Did THIS request pass a real signature / token check (not merely auth off)?"""
    return getattr(getattr(conn, "state", None), VERIFIED_STATE_ATTR, False) is True


def _forbidden() -> Response:
    return PlainTextResponse("Forbidden", status_code=403)


async def _authenticate_ws(websocket: WebSocket, surface: str, path: str) -> bool:
    """Verify a WS upgrade BEFORE accept(); closes the handshake with 403 on failure."""
    try:
        verified = verify_ws_upgrade(websocket, _settings, path)
    except WebhookAuthError as e:
        await audit_rejection(surface, client_ip(websocket), e.reason)
        await websocket.close(code=1008, reason="Unauthorized")
        return False
    _mark_verified(websocket, verified)
    return True


def _twiml_response(twiml: str) -> Response:
    return Response(content=twiml, media_type="text/xml")


@twilio_router.get("/health")
async def voice_health() -> dict[str, Any]:
    """Health check for Twilio webhook validation."""
    active = {}
    if _engine:
        active = {sid: s.direction.value for sid, s in _engine.get_active_calls().items()}
    return {
        "status": "ok",
        "engine": _settings.voice_engine if _settings else "unconfigured",
        "active_calls": len(active),
    }


@twilio_router.post("/webhook")
async def voice_webhook(request: Request) -> Response:
    """Inbound call handler — returns TwiML to start a stream or ConversationRelay."""
    if await _authenticate(request) is None:
        return _forbidden()

    if not _engine or not _settings:
        return _twiml_response("<Response><Say>Voice system is not configured.</Say><Hangup/></Response>")

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    caller = str(form.get("From", ""))
    called = str(form.get("To", ""))

    logger.info("Inbound call: %s from %s to %s", call_sid, caller, called)

    allowed = _settings.voice_allowed_callers.strip()
    if allowed != "*":
        allowed_set = {n.strip() for n in allowed.split(",")}
        if caller not in allowed_set:
            logger.warning("Rejected call from %s (not in allowlist)", caller)
            # This returns before `telemetry.inbound_webhook()` below, so without
            # recording it here the call exists in no dashboard, no metric and no
            # voice_calls row — an operator asking "why is call X missing" finds
            # nothing at all. The receptionist declines already do this via
            # `_receptionist_decline`; the allowlist was the one path that did not.
            #
            # `BLOCKED` rather than a new failure code: both mean "this caller is
            # not permitted", it is already in POLICY_BLOCKED (so it categorises
            # as policy_declined and stays out of the SLO, which is right — the
            # guardrail worked), and `reason` carries the distinction the operator
            # actually needs, since the remedy differs (allowlist vs blocklist).
            from pincer.observability.failure_codes import FailureCode
            from pincer.voice.language import resolve_call_language as _resolve_language

            await _record_declined_call(
                call_sid,
                caller,
                str(FailureCode.BLOCKED),
                _resolve_language(_settings),
                reason="not_allowlisted",
            )
            return _twiml_response("<Response><Say>This number is not authorized.</Say><Hangup/></Response>")

    from pincer.voice.engine import CallDirection
    from pincer.voice.language import resolve_call_language
    from pincer.voice.twiml_builder import build_connect_twiml

    call_language = resolve_call_language(_settings)

    # Sprint 12: receptionist line — blocklist, capacity, profile language
    from pincer.voice.receptionist.profile import get_profile, receptionist_active

    if receptionist_active(_settings):
        profile = get_profile()
        assert profile is not None
        call_language = resolve_call_language(_settings, profile.default_language)
        declined = await _receptionist_decline(call_sid, caller, call_language)
        if declined is not None:
            return declined

    from pincer.voice.telemetry import hooks as telemetry

    telemetry.inbound_webhook(
        call_sid,
        from_number=caller,
        to_number=called,
        engine=str(getattr(_settings, "voice_engine", "") or ""),
        language=call_language,
    )
    await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND, language=call_language)

    twiml = build_connect_twiml(
        _settings,
        call_sid=call_sid,
        direction="inbound",
        language=call_language,
        counterparty=caller,
    )
    return _twiml_response(twiml)


def _blocklist(settings: Any) -> set[str]:
    raw = str(getattr(settings, "voice_blocklist", "") or "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _active_inbound_count() -> int:
    if _engine is None:
        return 0
    from pincer.voice.engine import CallDirection

    # A call bridged to a human still counts: a failed transfer reconnects it
    # to the relay, and its slot must still be free when it does.
    return sum(1 for st in _engine.get_active_calls().values() if st.direction == CallDirection.INBOUND)


async def _receptionist_decline(call_sid: str, caller: str, language: str) -> Response | None:
    """§10.2 blocklist / §10.3 concurrency: one neutral sentence + <Hangup/>,
    a voice_calls row with the failure code, and a metric. None = proceed."""
    from xml.sax.saxutils import escape

    from pincer.observability.failure_codes import FailureCode
    from pincer.voice.language import relay_language
    from pincer.voice.prompts import get_prompt

    assert _settings is not None
    lines = get_prompt("RECEPTIONIST_LINES", language) or {}
    code: FailureCode | None = None
    line = ""
    if caller and caller in _blocklist(_settings):
        code, line = FailureCode.BLOCKED, str(lines.get("blocked", ""))
        logger.warning("Inbound call %s from blocklisted caller declined", call_sid)
    elif _active_inbound_count() >= int(getattr(_settings, "inbound_max_concurrent", 3) or 3):
        code, line = FailureCode.BUSY_CAPACITY, str(lines.get("busy", ""))
        logger.warning("Inbound call %s declined: capacity %s reached", call_sid, _active_inbound_count())
    if code is None:
        return None
    await _record_declined_call(call_sid, caller, str(code), language)
    say = f'<Say language="{relay_language(language)}">{escape(line)}</Say>' if line else ""
    return _twiml_response(f"<Response>{say}<Hangup/></Response>")


async def _record_declined_call(
    call_sid: str, caller: str, failure_code: str, language: str, reason: str = "receptionist_policy"
) -> None:
    """Declined calls never reach the engine, so the row + metric are written here."""
    try:
        from pincer.observability.metrics import record_call_ended, record_inbound_event
        from pincer.voice.telemetry import hooks as telemetry

        await telemetry.call_declined(call_sid, failure_code=failure_code, reason=reason, language=language)
        record_inbound_event(failure_code, language=language)
        record_call_ended(direction="inbound", outcome="failed", failure_code=failure_code, language=language)
    except Exception:
        logger.debug("declined-call metrics failed", exc_info=True)
    if _settings is None:
        return
    try:
        from pincer.services.voice import CallsService

        now = datetime.now(UTC).isoformat()
        calls = await CallsService.for_path(Path(str(_settings.db_path)))
        await calls.save_call(
            {
                "call_sid": call_sid,
                "direction": "inbound",
                "from_number": caller,
                "to_number": str(getattr(_settings, "twilio_phone_number", "") or ""),
                "started_at": now,
                "ended_at": now,
                "failure_code": failure_code,
                "engine": str(getattr(_settings, "voice_engine", "") or ""),
                "language": language,
            }
        )
    except Exception:
        logger.debug("declined-call row failed [%s]", call_sid, exc_info=True)


@twilio_router.post("/transfer-result")
async def voice_transfer_result(request: Request) -> Response:
    """Sprint 12 §8.4: <Dial action> callback after a receptionist transfer.

    completed / answered / DialBridged=true → the caller talked to the human:
    record it and finalize the call (Twilio has ended it; no REST hangup).
    busy / no-answer / failed / canceled → apology + message-taking: the call
    is reconnected to the relay with the apology as its greeting and the
    session already in TAKE_MESSAGE (name question asked) — unless the caller
    already hung up while the target was ringing, in which case there is
    nothing to reconnect. Any other status (empty, unknown) counts as failed
    while the caller is on the line; once the call is completed it is not
    guessed at: the call is finalized and the report says the transfer started.
    No state with the caller still on the line (process restarted while the
    target rang) → re-register the call, resume its new receptionist session in
    TAKE_MESSAGE, and reconnect with the apology. If Twilio cannot be asked
    whether the caller is still there, the callback's own CallStatus decides.
    Idempotent, one delivery per call at a time: a callback for a finalized
    call, or one Twilio says is no longer in progress, only hangs up; a copy of
    a result already processed counts nothing, leaves the session alone, and
    is answered from its dial's record (one per dial in ``DIALS_KEY``, keyed by
    the ``dial`` token `transfer_call` puts in the action URL, else by
    ``DialCallSid``; see `dial_key`): the recorded reconnect TwiML until the
    reconnected relay connects, an empty ``<Response/>`` after that or for an
    earlier dial (`_answer_copy`). The current dial's result is handled even when the
    call is not marked transferring: the redirect can raise after Twilio
    applied it, and then Twilio is waiting on this answer.

    Why an empty answer is safe there: Twilio executes at most one action
    response per <Dial>. The empty answer is given only when this dial's
    reconnected relay has connected (proof that Twilio executed our answer for
    it) or a later dial exists (which needed that answer executed first), so
    Twilio will not execute it. No other TwiML would be safer: if executed, any
    of it would replace the live relay. A retry that Twilio does execute comes
    before any relay connected, and the per-call lock puts it behind the first
    delivery, so it gets the recorded reconnect. That holds under Twilio's
    default connection overrides (ct=5000ms, rt=15000ms, tt=15000ms, rc=1,
    rp=ct: a retry only after a failed TCP connect or TLS handshake; see
    https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides)
    and under read-timeout retries or a proxy that re-sends the POST alike.
    `transfer_call` still refuses an action URL with overrides (``#…``)."""
    if await _authenticate(request) is None:
        return _forbidden()
    form = await request.form()
    # One delivery of a call's dial result at a time: a copy arriving while the
    # first is still awaiting (Twilio's status, the call's registration, the
    # session) waits, then finds that work done and replays its answer.
    async with one_call_update_at_a_time(str(form.get("CallSid", ""))):
        return await _handle_transfer_result(form, str(request.query_params.get("dial", "")))


async def _handle_transfer_result(form: Any, dial_id: str) -> Response:
    from pincer.voice.engine import DIAL_ID_KEY, dial_key

    call_sid = str(form.get("CallSid", ""))
    dial_status = str(form.get("DialCallStatus", "")).lower()
    call_status = str(form.get("CallStatus", "")).lower()
    dial_call_sid = str(form.get("DialCallSid", ""))
    dial_duration = _int_or_none(form.get("DialCallDuration"))
    dial_bridged = str(form.get("DialBridged", "")).lower() == "true"
    bridged = dial_status in ("completed", "answered") or dial_bridged
    # The dial's result as its record keeps it (`_record_dial_result`).
    result: dict[str, Any] = {
        "dial_id": dial_id,
        "dial_status": dial_status,
        "dial_call_sid": dial_call_sid,
        "dial_duration_s": dial_duration,
        "dial_bridged": dial_bridged,
    }
    logger.info("Transfer result [%s]: %s (call %s)", call_sid, dial_status or "-", call_status or "-")
    hangup = _twiml_response("<Response><Hangup/></Response>")
    no_op = _twiml_response("<Response/>")
    if not _engine or not _settings:
        return hangup
    state = _engine.get_call_state(call_sid)
    if state is None:
        if bridged or call_status == "completed":
            logger.info("Transfer result [%s] for a call already finalized — nothing to do", call_sid)
            return hangup
        if _already_torn_down(call_sid):
            # A late/replayed callback for a call we ended: re-registering it
            # would create a phantom call that never gets media and holds a
            # capacity slot until the stuck-call reaper pages.
            logger.info("Transfer result [%s] for a call already torn down — nothing to do", call_sid)
            return hangup
        # The in-memory record above is gone after a restart, after
        # _ENDED_CALL_TTL_S, or after _MAX_ENDED_CALLS other calls: Twilio is
        # the authority on whether the caller is still on the line.
        try:
            twilio_status = await _engine.fetch_call_status(call_sid)
        except Exception:
            # Twilio cannot be asked: go by the callback itself, which says the
            # caller is on the line. Hanging up would drop a live caller with no
            # trace or report; a phantom call (a replay during a Twilio outage)
            # is the lesser risk, and the stuck-call reaper bounds it.
            logger.exception("Transfer result [%s]: Twilio status check failed — trusting the callback", call_sid)
            twilio_status = "in-progress"
        if twilio_status != "in-progress":
            logger.info("Transfer result [%s] for a call Twilio reports %r — nothing to do", call_sid, twilio_status)
            return hangup
        # Call state lost (restart/redeploy while the target rang), but the
        # caller is still on the line: fall back to message-taking.
        logger.warning("Transfer result [%s] with no call state — reconnecting the caller", call_sid)
        from pincer.voice.engine import CallDirection
        from pincer.voice.language import resolve_call_language
        from pincer.voice.receptionist.profile import get_profile, receptionist_active
        from pincer.voice.telemetry import hooks as telemetry

        profile = get_profile() if receptionist_active(_settings) else None
        language = resolve_call_language(_settings, profile.default_language if profile else "")
        caller = str(form.get("From", ""))
        # Register the call here (this request is signature-verified, like
        # /webhook): the relay `setup` for an unknown SID is refused, so without
        # this the caller would hear the apology and then be dropped.
        telemetry.inbound_webhook(
            call_sid,
            from_number=caller,
            to_number=str(form.get("To", "")),
            engine=str(getattr(_settings, "voice_engine", "") or ""),
            language=language,
        )
        try:
            # No capacity, allowlist or blocklist check, on purpose: this caller
            # was already admitted by /webhook before the transfer started.
            recovered = await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND, language=language)
            # Record the dial result on the recovered state before the next
            # await, so a retry of this callback replays the reconnect instead
            # of taking the main path. (The per-call lock already keeps a
            # concurrent copy out until this request is done.)
            from pincer.voice.receptionist.session import TRANSFER_DIAL_TIMEOUT_S

            # The lost dial was the receptionist's: its target and dial timeout
            # are the profile's and the session's, so its start can name them.
            result = {
                **result,
                "target": str(profile.transfer.target if profile is not None else ""),
                "dial_timeout_s": TRANSFER_DIAL_TIMEOUT_S,
                "recovered": True,
            }
            record: dict[str, Any] = dict(result)
            if isinstance(getattr(recovered, "metadata", None), dict):
                record = _record_dial_result(recovered, dial_key(dial_id, dial_call_sid), result)
                if dial_id:
                    recovered.metadata[DIAL_ID_KEY] = dial_id
            # Once, on the trace just opened: a retry replays the reconnect below
            # the state check and records nothing.
            telemetry.transfer_result(call_sid, dial_status, dial_duration_s=dial_duration)
            # The channel started a fresh receptionist session (RECEPTION_INTENT);
            # resume it in TAKE_MESSAGE, as a failed transfer does, so the first
            # turn asks for the name instead of greeting the caller again.
            session = _transfer_session_lookup(call_sid)
            greeting, response = await _resume_failed_transfer(call_sid, session, dial_status, language)
            _record_reconnect(record, greeting, language)
            return response
        except Exception:
            # Twilio drops the caller if we answer 500, and a registered call
            # that never gets media holds a capacity slot until the stuck-call
            # reaper pages: hang up and finalize whatever got registered.
            logger.exception("Transfer result [%s]: recovery failed — finalizing the recovered call", call_sid)
            await _finalize_failed_recovery(call_sid)
            return hangup

    from pincer.voice.telemetry import hooks as telemetry

    language = str(getattr(state, "language", "") or "en")
    key, record = _dial_record_for(state, dial_id, dial_call_sid)
    if record is not None and "dial_status" in record:
        # A copy of a result we already processed: count nothing twice, leave
        # the session alone, and answer from the dial's record.
        is_current = record is state.metadata.get("transfer")
        return _answer_copy(call_sid, key, record, is_current=is_current, language=language)
    transferring = bool(state.metadata.get("transferring"))
    if dial_id and dial_id != state.metadata.get(DIAL_ID_KEY):
        # A later dial replaced this one, which needs the call back from this
        # dial first: Twilio is not waiting on this answer.
        logger.info("Transfer result [%s] for an earlier dial %s — ignored", call_sid, key)
        return no_op
    if not transferring:
        if not dial_id:
            # No dial token (an action URL from before tokens): this may be a
            # stray copy, or the result Twilio is waiting on after a redirect
            # that raised although Twilio applied it. Answer so that both are
            # safe: Twilio executes the answer only in the second case, where
            # the caller needs it. Count nothing and leave the session alone.
            logger.warning("Transfer result [%s] without a dial token for a call not being transferred", call_sid)
            if bridged:
                return hangup
            return _reconnect_after_transfer(call_sid, language, _transfer_failed_line(language))
        # The current dial's own result, yet the call is not marked
        # transferring: transfer_call's redirect raised although Twilio
        # applied it. Twilio is waiting on this answer: handle it as the
        # dial's result. An empty answer would end the call.
        logger.warning("Transfer result [%s] for dial %s whose redirect reported failure — handling it", call_sid, key)
    record = _record_dial_result(state, key, result)
    telemetry.transfer_result(call_sid, dial_status, dial_duration_s=dial_duration)
    session = _transfer_session_lookup(call_sid)

    if bridged:
        if session is not None:
            session.on_transfer_completed(dial_duration)
        await _engine.finish_remote_ended(call_sid, reason="transferred")
        return hangup

    unknown_result = dial_status not in _TRANSFER_RESULTS
    if call_status == "completed":
        if unknown_result:
            # The call is over and the dial result is unknown: don't guess.
            logger.warning("Transfer result [%s]: unknown DialCallStatus %r — finalizing", call_sid, dial_status)
            await _engine.finish_remote_ended(call_sid, reason="transfer_result_unknown")
            return hangup
        # The caller hung up while the target was ringing.
        if session is not None:
            session.on_transfer_abandoned()
        await _engine.finish_remote_ended(call_sid, reason="caller_hangup_during_transfer")
        return hangup

    if unknown_result:
        # Not bridged and the caller is still on the line: whatever the status
        # means, nobody took the call, so it is a failed transfer.
        logger.warning("Transfer result [%s]: unknown DialCallStatus %r — treating as failed", call_sid, dial_status)
    greeting, response = await _resume_failed_transfer(call_sid, session, dial_status, language)
    _record_reconnect(record, greeting, language)
    return response


def _dial_record_for(state: Any, dial_id: str, dial_call_sid: str) -> tuple[str, dict[str, Any] | None]:
    """The key and the record (if any) a dial result belongs to.

    With a dial token, the token's record. Without one (an action URL from
    before tokens): a recorded result with the same DialCallSid, else, while a
    <Dial> is in flight, the current dial's record."""
    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY, dial_key

    dials: dict[str, dict[str, Any]] = state.metadata.setdefault(DIALS_KEY, {})
    if dial_id:
        key = dial_key(dial_id, "")
        return key, dials.get(key)
    current = state.metadata.get("transfer")
    for key, record in dials.items():
        # an empty DialCallSid only matches the current dial's result
        recorded = "dial_status" in record and record.get("dial_call_sid") == dial_call_sid
        if recorded and (dial_call_sid or record is current):
            return key, record
    current_id = str(state.metadata.get(DIAL_ID_KEY, "") or "")
    if state.metadata.get("transferring") and current_id and isinstance(current, dict):
        return dial_key(current_id, ""), current
    return dial_call_sid, None


def _record_dial_result(state: Any, key: str, result: dict[str, Any]) -> dict[str, Any]:
    """Write a dial's result into its record (creating one for a dial we have
    none for: lost state, or no dial token) and make it the current dial's
    `transfer` view. The one place both the lost-state branch and the main path
    record a result, so the replay rules read the same fields from either.

    A result is proof its <Dial> ran, so the dial's start is recorded here when
    nothing confirmed it before (a slow or failed redirect answer, lost state);
    `note_transfer_started` records it once per dial."""
    from pincer.voice.engine import DIALS_KEY, note_transfer_started

    record: dict[str, Any] = state.metadata.setdefault(DIALS_KEY, {}).setdefault(key, {})
    record.update(result)
    state.metadata["transfer"] = record
    note_transfer_started(str(state.call_sid), record)
    return record


def _record_reconnect(record: dict[str, Any], greeting: str, language: str) -> None:
    """What a copy of this result is answered with: the reconnect is rebuilt
    from these, not stored as TwiML, because its relay URL carries a signed
    token that expires (`voice_signature_max_age_s`)."""
    record["reconnect_greeting"] = greeting
    record["reconnect_language"] = language


def _answer_copy(call_sid: str, key: str, record: dict[str, Any], *, is_current: bool, language: str) -> Response:
    """The answer to a copy of a dial result we already processed.

    The recorded reconnect, rendered again (a fresh signed relay URL: the first
    answer's token may be stale by now), while it is the current dial's and no
    relay has connected for it: Twilio may be executing this copy. An empty
    ``<Response/>`` once the relay connected, or for an earlier dial: Twilio
    has already executed an answer for that <Dial> and executes at most one
    per <Dial>. No TwiML would be harmless here if it were executed: any of it
    replaces the live relay."""
    if is_current and not record.get("reconnected"):
        logger.info("Transfer result [%s] already handled (dial %s) — replaying reconnect", call_sid, key)
        if "reconnect_greeting" in record:
            language = str(record.get("reconnect_language", "") or language)
            return _reconnect_after_transfer(call_sid, language, str(record["reconnect_greeting"]))
        # the first delivery raised before its answer was recorded
        return _reconnect_after_transfer(call_sid, language, _transfer_failed_line(language))
    logger.info("Transfer result [%s] for dial %s already acted on — ignored", call_sid, key)
    return _twiml_response("<Response/>")


async def _resume_failed_transfer(call_sid: str, session: Any, dial_status: str, language: str) -> tuple[str, Response]:
    """Nobody took the call and the caller is still on the line: resume the
    receptionist session in TAKE_MESSAGE (apology + name question), take the
    call out of TRANSFERRING, and build the reconnect TwiML. Returns the
    greeting too, so the caller can record it for a retried callback to replay."""
    try:
        if session is not None:
            session.transfer_attempted = True  # the report says the transfer did not connect
            greeting = await session.on_transfer_failed(_TRANSFER_RESULTS.get(dial_status, "failed"))
        else:
            greeting = _transfer_failed_line(language)
    finally:
        # The dial is over: the call is ours again now, not when (or if) the
        # reconnected relay/stream connects. This also takes the phase out of
        # TRANSFERRING when there was no session to do it (no-op for a call
        # recovered from lost state, which was never marked transferring).
        # In `finally` so a failing session step cannot leave the call marked
        # transferring: on_media_closed would then ignore the closing socket
        # and the reaper would apply the long transfer allowance.
        await _engine.end_transfer(call_sid)
    return greeting, _reconnect_after_transfer(call_sid, language, greeting)


def _mark_relay_webhook(call_sid: str) -> None:
    """The call's events come over the deprecated HTTP relay: see RELAY_WEBHOOK_KEY."""
    from pincer.voice.engine import RELAY_WEBHOOK_KEY

    state = _engine.get_call_state(call_sid) if _engine and call_sid else None
    metadata = getattr(state, "metadata", None)
    if isinstance(metadata, dict):
        metadata[RELAY_WEBHOOK_KEY] = True


def _note_reconnected(state: Any) -> None:
    """Media connected for a call that has a transfer record: Twilio executed the
    reconnect after a failed dial. The `transfer` view only exists from
    `transfer_call` on, and moves to the next dial's own record, so this marks
    the current dial's. A replay of that dial's result must no longer answer
    with <Connect>."""
    transfer = getattr(state, "metadata", {}).get("transfer") if state is not None else None
    if isinstance(transfer, dict):
        transfer["reconnected"] = True


def _transfer_failed_line(language: str) -> str:
    """The apology for a failed transfer, without a session to phrase it."""
    from pincer.voice.prompts import get_prompt

    lines = get_prompt("RECEPTIONIST_LINES", language) or {}
    return str(lines.get("transfer_failed", ""))


async def _finalize_failed_recovery(call_sid: str) -> None:
    """A lost-state call whose recovery raised: finalize it as a failed call
    with its own code (its session sits in a benign phase, so a plain finalize
    would count the hang-up as a completed call). Never raises, so the handler
    still answers with a hang-up. The trace opened by `inbound_webhook` is
    closed here when the end-of-call path did not close it: nothing was
    registered, or finalizing raised."""
    from pincer.observability.failure_codes import FailureCode
    from pincer.voice.engine import set_end_cause
    from pincer.voice.telemetry import hooks as telemetry
    from pincer.voice.telemetry.outcomes import STATUS_FAILED

    assert _engine is not None
    code = FailureCode.TRANSFER_RECOVERY_FAILED
    state = _engine.get_call_state(call_sid)
    if state is not None:
        set_end_cause(state, code)
        try:
            await _engine.finish_remote_ended(call_sid, reason=str(code))
            return
        except Exception:
            # The call may stay registered: the stuck-call reaper is the backstop.
            logger.exception("Transfer result [%s]: finalizing the failed recovery failed", call_sid)
    await telemetry.call_ended(call_sid, status=STATUS_FAILED, failure_code=str(code), termination_reason=str(code))


def _reconnect_after_transfer(call_sid: str, language: str, greeting: str) -> Response:
    """ConversationRelay TwiML for a caller back from a failed transfer, with
    ``greeting`` (if any) replacing the welcome greeting."""
    from xml.sax.saxutils import escape

    from pincer.voice.twiml_builder import build_connect_twiml

    twiml = build_connect_twiml(_settings, call_sid=call_sid, direction="inbound", language=language)
    if greeting:
        # Replace the welcome greeting with the apology + first question
        import re as _re

        escaped = escape(greeting, {'"': "&quot;"})
        if "welcomeGreeting=" in twiml:
            # A function, not a template: a `\` in the greeting stays text
            twiml = _re.sub(r'welcomeGreeting="[^"]*"', lambda _m: f'welcomeGreeting="{escaped}"', twiml, count=1)
        else:
            twiml = twiml.replace("<ConversationRelay ", f'<ConversationRelay welcomeGreeting="{escaped}" ', 1)
    return _twiml_response(twiml)


# DialCallStatus → the `result` label of pincer.voice.receptionist.transfers
_TRANSFER_RESULTS = {"busy": "busy", "no-answer": "no_answer", "failed": "failed", "canceled": "failed"}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


_transfer_session_resolver: Any = None


def set_transfer_session_resolver(resolver: Any) -> None:
    """Channel hook: call_sid → ReceptionSession (for the dial-result fallback)."""
    global _transfer_session_resolver  # noqa: PLW0603
    _transfer_session_resolver = resolver


def _transfer_session_lookup(call_sid: str) -> Any:
    if _transfer_session_resolver is None:
        return None
    try:
        return _transfer_session_resolver(call_sid)
    except Exception:
        return None


def _report_language(call_sid: str) -> str:
    """The language the initiating user gets their call reports in (Sprint 3)."""
    from pincer.voice.status_notify import get_call_language

    call_lang = get_call_language(call_sid).strip().lower()[:2]
    return call_lang if call_lang in ("de", "uk") else "en"


def _already_torn_down(call_sid: str) -> bool:
    """Whether the engine tore this call down recently (`was_recently_ended`).

    Two callers rely on it:

    - The status and AMD webhooks: from teardown on, the post-call pipeline (a
      background task) owns the final user message, so a webhook that finds no
      live state must not send its own generic ending. That consumes the ENDED
      stage and the real report is dropped.
    - /transfer-result's lost-state branch: a replayed callback for a call we
      ended must not re-register it as a phantom INBOUND call that never gets
      media and holds a capacity slot until the stuck-call reaper pages.

    The record is in memory only: it is lost on restart, forgotten after
    `_ENDED_CALL_TTL_S` (900 s), and evicted after `_MAX_ENDED_CALLS` (500)
    newer teardowns. A miss therefore does not mean the call is live; the
    transfer branch asks Twilio (`fetch_call_status`) before recovering. Narrow
    this for one caller and the other changes too.
    """
    if _engine is None:
        return False
    return bool(getattr(_engine, "was_recently_ended", lambda _sid: False)(call_sid))


async def _handle_amd_verdict(call_sid: str, answered_by: str, user_lang: str) -> bool:
    """Act on an Answering Machine Detection verdict.

    Returns True when the verdict was acted on (the call was hung up), False
    when the caller is a human, the verdict is empty, or it was overruled —
    in which case the caller must fall through to its normal handling.

    The verdict reaches us on /amd for outbound calls (async AMD) and on
    /status for anything still running AMD synchronously.
    """
    if not (answered_by.startswith("machine") or answered_by == "fax"):
        return False

    from pincer.voice.status_notify import notify_ended

    amd_state = _engine.get_call_state(call_sid) if _engine else None
    if amd_state is not None and amd_state.metadata.get("caller_spoke"):
        # AMD false positive: a human is already mid-conversation. Acting
        # on the verdict here killed live calls 20s in — ignore it and let
        # the caller fall through to normal handling.
        logger.info(
            "AMD verdict '%s' ignored [%s] — caller already conversing",
            answered_by,
            call_sid,
        )
        return False

    logger.info("Voicemail/machine detected [%s] (%s) — hanging up", call_sid, answered_by)
    from pincer.observability.failure_codes import FailureCode
    from pincer.voice.engine import set_end_cause
    from pincer.voice.outcome import not_connected_reason

    # Appointment calls (Sprint 6): voicemail triggers the retry
    # policy. Consumes the scheduling context BEFORE end_call so the
    # post-call pipeline doesn't double-handle it.
    if _settings is not None:
        from pincer.voice.scheduling import handle_call_not_connected

        await handle_call_not_connected(call_sid, "voicemail", _settings)
    vm_state = _engine.get_call_state(call_sid) if _engine else None
    if vm_state is not None:
        # The post-call pipeline owns the final message: sending the
        # ENDED stage here would pop the call from tracking and the
        # structured report would be built but never delivered. Only the
        # code is stored; the report renders it in the report language.
        set_end_cause(vm_state, FailureCode.VOICEMAIL)
    elif _already_torn_down(call_sid):
        # The state was torn down moments ago, not absent all along: end_call
        # already ran and its post-call pipeline owns the final message.
        logger.info("AMD verdict [%s] — call already torn down, leaving the report to the pipeline", call_sid)
    else:
        # No conversation state -> end_call fires no post-call pipeline
        await notify_ended(call_sid, not_connected_reason(FailureCode.VOICEMAIL, user_lang))
    if _engine:
        # The call is live (greeting a voicemail box) — always hang up
        await _engine.end_call(call_sid)
    return True


@twilio_router.post("/amd")
async def voice_amd(request: Request) -> PlainTextResponse:
    """Asynchronous Answering Machine Detection result.

    Outbound calls are dialled with AsyncAmd=true. With Twilio's default
    (synchronous) AMD the call is *blocked* until detection finishes, so the
    callee picks up and hears nothing at all — no welcomeGreeting, no relay —
    for up to `machine_detection_timeout` seconds. Async AMD connects the
    callee immediately and delivers the verdict here instead, which is the
    only thing /status ever used it for.
    """
    if await _authenticate(request) is None:
        return PlainTextResponse("Forbidden", status_code=403)

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    answered_by = str(form.get("AnsweredBy", ""))
    logger.info("AMD result [%s]: %s", call_sid, answered_by or "(none)")
    from pincer.voice.telemetry import hooks as telemetry

    telemetry.amd_verdict(call_sid, answered_by)
    await _handle_amd_verdict(call_sid, answered_by, _report_language(call_sid))
    return PlainTextResponse("OK")


@twilio_router.post("/status")
async def voice_status(request: Request) -> PlainTextResponse:
    """Call status callbacks (ringing, answered, completed) + AMD results."""
    if await _authenticate(request) is None:
        return PlainTextResponse("Forbidden", status_code=403)

    from pincer.voice.status_notify import notify_connected, notify_ended

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    status = str(form.get("CallStatus", ""))
    duration = form.get("CallDuration", "0")
    answered_by = str(form.get("AnsweredBy", ""))

    logger.info(
        "Call status: %s -> %s (duration=%s%s)",
        call_sid,
        status,
        duration,
        f", answered_by={answered_by}" if answered_by else "",
    )

    from pincer.voice.telemetry import hooks as telemetry

    # `SequenceNumber` is Twilio's own per-call ordinal for status callbacks, so a
    # retried callback dedupes to the row it already wrote rather than appearing
    # twice on the timeline.
    telemetry.provider_status(
        call_sid,
        status,
        sequence=str(form.get("SequenceNumber", "") or status),
        duration_s=duration,
        answered_by=answered_by or None,
    )

    # Reports reach the user in the language of their initiating command (Sprint 3)
    user_lang = _report_language(call_sid)

    # T1.3: Answering Machine Detection — voicemail is reported, not conversed with.
    if await _handle_amd_verdict(call_sid, answered_by, user_lang):
        return PlainTextResponse("OK")

    # A terminal status waits for a /transfer-result in flight for the call: one
    # recovering the call from lost state checks Twilio's status first, and a
    # hang-up landing between that check and the registration must find the
    # registered call and finalize it, not miss it and leave a phantom.
    terminal = status in ("busy", "no-answer", "failed", "canceled", "completed")
    async with one_call_update_at_a_time(call_sid) if terminal else contextlib.nullcontext():
        if status == "in-progress":
            # Callee (a human) picked up
            await notify_connected(call_sid)

        elif status in ("busy", "no-answer", "failed", "canceled"):
            # Call never connected — the user still gets a final status (T1.3/T1.5)
            from pincer.observability.failure_codes import NOT_CONNECTED_STATUS
            from pincer.voice.engine import END_CODE_KEY, PROVIDER_STATUS_KEY, set_end_cause
            from pincer.voice.outcome import not_connected_reason

            end_code = NOT_CONNECTED_STATUS[status]
            # Appointment calls (Sprint 6): busy/no-answer/failed triggers the retry policy
            if _settings is not None:
                from pincer.voice.scheduling import handle_call_not_connected

                await handle_call_not_connected(call_sid, status, _settings)
            nc_state = _engine.get_call_state(call_sid) if _engine else None
            if _engine is not None and nc_state is not None:
                # Conversation state exists (e.g. a late failure) -> the post-call
                # pipeline sends the final report; pre-empting it here would
                # consume the ENDED stage and drop that report. Only the code is
                # stored; the report renders it in the report language.
                if END_CODE_KEY not in nc_state.metadata:
                    # Only when this status is the cause: first writer wins.
                    nc_state.metadata[PROVIDER_STATUS_KEY] = status
                set_end_cause(nc_state, end_code)
                await _engine.end_call(call_sid)
            elif _already_torn_down(call_sid):
                logger.info("Status '%s' [%s] — call already torn down, pipeline owns the report", status, call_sid)
            else:
                # The call never had state, so no pipeline will run
                await notify_ended(call_sid, not_connected_reason(end_code, user_lang, status))

        elif status == "completed" and _engine:
            state = _engine.get_call_state(call_sid)
            if state:
                # Twilio has already ended the call: finalize without a REST
                # hangup, which could only fail.
                await _engine.finish_remote_ended(call_sid, reason="provider_completed")
            elif _already_torn_down(call_sid):
                # Race: end_call (AMD hangup, caller hangup, shutdown) popped the
                # state and handed the final message to its post-call pipeline,
                # which runs as a background task. Sending the generic ending here
                # consumes the ENDED stage first, and the real report — the
                # voicemail outcome, the summary — is then silently dropped.
                logger.info("Status 'completed' [%s] — call already torn down, pipeline owns the report", call_sid)
            else:
                # Ended before any conversation state existed (e.g. hangup during AMD)
                ended_texts = {"en": "call ended", "de": "Anruf beendet", "uk": "дзвінок завершено"}
                await notify_ended(call_sid, ended_texts[user_lang])

    return PlainTextResponse("OK")


@twilio_router.post("/fallback")
async def voice_fallback(request: Request) -> Response:
    """Error fallback — plays apology message in the call language, logs error."""
    if await _authenticate(request) is None:
        return _forbidden()

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    error_code = str(form.get("ErrorCode", ""))
    error_msg = str(form.get("ErrorMessage", ""))

    logger.error(
        "Voice fallback triggered: call=%s code=%s msg=%s",
        call_sid,
        error_code,
        error_msg,
    )
    from pincer.voice.telemetry import hooks as telemetry

    telemetry.error(call_sid, "twiml_error", stage="provider", provider_error_code=error_code)

    from pincer.voice.status_notify import get_call_language

    fallback_lang = get_call_language(call_sid).strip().lower()[:2]
    if fallback_lang == "de":
        apology = (
            '<Say language="de-DE">Entschuldigung, es gibt gerade ein technisches Problem. '
            "Bitte versuchen Sie es später noch einmal.</Say>"
        )
    elif fallback_lang == "uk":
        apology = '<Say language="uk-UA">Вибачте, сталася технічна проблема. Будь ласка, спробуйте пізніше.</Say>'
    else:
        apology = "<Say>I'm sorry, I'm experiencing technical difficulties. Please try again later.</Say>"

    return _twiml_response(f"<Response>{apology}<Hangup/></Response>")


@twilio_router.websocket("/relay")
async def relay_ws(websocket: WebSocket) -> None:
    """ConversationRelay WebSocket — the TwiML `<ConversationRelay url="wss://.../relay">` target.

    ConversationRelay is WebSocket-only: Twilio rejects an https URL with
    error 64101 ("Invalid url parameter value") at <Connect> time, which
    surfaces to the caller as "an application error has occurred" right after
    the greeting. Twilio sends JSON messages here (setup / prompt / interrupt /
    error); the engine speaks by sending {"type": "text", "token": ..., "last":
    true} back over this socket (ConversationRelayEngine.send_speech).
    """
    if not await _authenticate_ws(websocket, "twilio_ws:relay", WS_RELAY_PATH):
        return

    await websocket.accept()

    if not _engine:
        await websocket.close(code=1011, reason="Engine not initialized")
        return

    call_sid = ""
    state = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = str(msg.get("type", ""))

            if msg_type == "setup":
                call_sid = str(msg.get("callSid", ""))
                caller = str(msg.get("from", ""))
                logger.info("ConversationRelay connected: %s from %s", call_sid, caller)
                # back from a <Dial> transfer: the call is ours again
                await _engine.end_transfer(call_sid)
                state = _engine.get_call_state(call_sid)
                if state is None:
                    state = await _resolve_setup_state(
                        call_sid,
                        caller,
                        str(msg.get("direction", "")),
                        client=client_ip(websocket),
                        to=str(msg.get("to", "")),
                        verified=_was_verified(websocket),
                    )
                    if state is None:
                        # Outbound with no retrievable briefing (terminated,
                        # never improvised, §2) or an unknown SID (refused —
                        # only /webhook and the dialer may create call state).
                        await websocket.close(code=1011)
                        return
                existing_ws = state.metadata.get("websocket")
                if (
                    existing_ws is not None
                    and existing_ws is not websocket
                    and getattr(existing_ws, "client_state", None) == WebSocketState.CONNECTED
                ):
                    # A live socket already speaks for this call. Overwriting
                    # it would redirect the agent's audio to whoever supplied
                    # this SID and leave the genuine caller silent.
                    await audit_rejection(
                        "twilio_ws:relay", client_ip(websocket), "live socket takeover refused", call_sid=call_sid
                    )
                    state = None
                    await websocket.close(code=1008, reason="Already connected")
                    return
                state.metadata["websocket"] = websocket
                from pincer.voice.telemetry import hooks as telemetry

                telemetry.media_open(call_sid, engine="conversation_relay")
                _note_reconnected(state)
                # `setup` is Twilio's "the callee picked up". Every clock in
                # the call starts from here — an outbound state exists from
                # before the dial, so registration is NOT an answer.
                await _engine.mark_call_answered(call_sid)

            elif msg_type == "prompt":
                text = str(msg.get("voicePrompt", ""))
                if text and call_sid:
                    await _engine.on_speech_input(call_sid, text)

            elif msg_type == "interrupt":
                # Twilio has already stopped its playback; this cancels the turn
                # still writing tokens behind it. Reached mid-turn only because
                # the prompt above hands its turn off instead of awaiting it.
                if call_sid:
                    heard = msg.get("utteranceUntilInterrupt")
                    await _engine.on_caller_barge_in(call_sid, str(heard) if heard is not None else None)

            elif msg_type == "error":
                logger.error("ConversationRelay error [%s]: %s", call_sid, msg)
                from pincer.voice.telemetry import hooks as telemetry

                telemetry.error(call_sid, "relay_error", stage="provider", detail=str(msg.get("description", ""))[:120])
                await _handle_relay_error(call_sid, msg)

    except WebSocketDisconnect:
        logger.info("ConversationRelay disconnected: %s", call_sid)
    except Exception:
        logger.exception("ConversationRelay WS error: %s", call_sid)
    finally:
        if state is not None and state.metadata.get("websocket") is websocket:
            state.metadata.pop("websocket", None)
            from pincer.voice.telemetry import hooks as telemetry

            telemetry.media_closed(call_sid, reason="relay_socket_closed")
            # socket gone = call over (inbound calls have no other end signal)
            await _media_closed(call_sid)


async def _resolve_setup_state(
    call_sid: str, caller: str, direction: str = "", client: str = "", to: str = "", *, verified: bool = False
) -> Any:
    """The call state a `setup` message belongs to, or None to refuse the call.

    Order matters, and each step exists because the previous one can lose a
    briefing:

    1. The state registered before the dial (the normal outbound path).
    2. The pending dial to this `to` number, claimed by the setup itself —
       Twilio can connect the callee before the REST dial has returned, and
       the dial's request timeout (10 s) is longer than any wait here. Only
       for a `verified` setup: `to` and `callSid` are socket-supplied, and
       with auth off anyone who knows the callee's number could take the
       briefing of a dial in flight. Unverified setups skip to step 3.
    3. A short wait — the promotion may be milliseconds away.
    4. The status_notify record, which also carries purpose and language.
    5. Give up. If Twilio told us this is an outbound API call, we return None
       and the caller hangs up: an outbound call whose briefing cannot be
       found MUST NOT run, because what it runs instead is a generic assistant
       persona on a call the user gave a task to.
    6. Otherwise refuse: an unknown SID on a voice socket is never a
       legitimate Twilio call. Genuine inbound calls are registered by the
       signature-verified /webhook before their TwiML (and hence this socket)
       exists, and genuine outbound calls leave a state or a status record.
       Creating state from socket-supplied callSid/from here would let anyone
       who reaches the endpoint mint a conversation with the agent under an
       attacker-chosen caller identity.
    """
    if _engine is None:
        return None
    from pincer.voice.engine import CallDirection
    from pincer.voice.status_notify import get_call_info

    is_outbound = str(direction or "").lower().startswith("outbound")

    state = _engine.get_call_state(call_sid)
    if state is not None:
        return state

    if is_outbound:
        state = await _engine.claim_pending_outbound(call_sid, to) if verified else None
        if state is None:
            state = await _engine.await_call_state(call_sid, timeout=SETUP_STATE_WAIT_S)
        if state is not None:
            return state

    info = get_call_info(call_sid)
    if info is not None and info.purpose:
        logger.info("Recovered outbound briefing for %s from the status record", call_sid)
        return await _engine.on_call_start(
            call_sid,
            info.target_number or caller,
            CallDirection.OUTBOUND,
            target_number=info.target_number,
            purpose=info.purpose,
            language=info.language,
            target_name=info.target_name,
            instructions=info.instructions,
        )

    if is_outbound:
        logger.error("outbound setup with no registered state [%s] — ending call", call_sid)
        await _record_briefing_lost(call_sid, info)
        return None

    logger.warning("setup for unknown call SID [%s] from %r refused", call_sid, caller)
    await audit_rejection("twilio_setup", client or "unknown", "unknown call SID", call_sid=call_sid)
    return None


async def _record_briefing_lost(call_sid: str, info: Any = None) -> None:
    """Persist and report an outbound call refused for a lost briefing.

    The user asked for a call and must be told it did not happen, with the
    same failure taxonomy every other terminated call uses — silence here
    would be indistinguishable from the bug this rule exists to prevent.
    """
    from pincer.observability.failure_codes import FailureCode

    language = str(getattr(info, "language", "") or "") if info is not None else ""
    try:
        from pincer.observability.metrics import record_call_ended

        record_call_ended(
            direction="outbound",
            outcome="failed",
            failure_code=str(FailureCode.BRIEFING_LOST),
            engine=str(getattr(_settings, "voice_engine", "") or ""),
            language=language,
            duration_s=0.0,
        )
    except Exception:
        logger.debug("briefing-lost metric failed", exc_info=True)

    if _settings is not None:
        try:
            from pincer.services.voice import CallsService

            now = datetime.now(UTC).isoformat()
            calls = await CallsService.for_path(Path(str(_settings.db_path)))
            await calls.save_call(
                {
                    "call_sid": call_sid,
                    "direction": "outbound",
                    "from_number": str(getattr(_settings, "twilio_phone_number", "") or ""),
                    "to_number": str(getattr(info, "target_number", "") or "") if info is not None else "",
                    "started_at": now,
                    "ended_at": now,
                    "failure_code": str(FailureCode.BRIEFING_LOST),
                    "engine": str(getattr(_settings, "voice_engine", "") or ""),
                    "language": language,
                }
            )
        except Exception:
            logger.debug("briefing-lost row failed [%s]", call_sid, exc_info=True)

    try:
        from pincer.voice.status_notify import notify_ended

        await notify_ended(
            call_sid,
            "the call was ended because the task you gave it could not be recovered — nothing was said. "
            "Please try again.",
        )
    except Exception:
        logger.debug("briefing-lost notify failed [%s]", call_sid, exc_info=True)


async def _media_closed(call_sid: str) -> None:
    """Tell the engine the media session closed (idempotent)."""
    hook = getattr(_engine, "on_media_closed", None) if _engine is not None and call_sid else None
    if hook is None:
        return
    try:
        await hook(call_sid)
    except Exception:
        logger.exception("on_media_closed failed [%s]", call_sid)


# Consecutive Twilio-side TTS failures (error 64111, "Error converting tokens
# to speech") tolerated before the call takes the spoken-apology fallback.
MAX_CR_TTS_ERRORS = 2


async def _handle_relay_error(call_sid: str, msg: dict[str, Any]) -> None:
    """Twilio-side TTS failure resilience: repeated 64111 means the configured
    voice cannot be synthesized by Twilio's ElevenLabs integration (e.g. a
    voice/language pairing Twilio doesn't carry). The caller is hearing pure
    silence while transcripts look healthy — mark the voice unusable so
    subsequent calls build Google-fallback TwiML, and end this call with the
    spoken apology instead of letting it stay silent.
    """
    if not _engine or not call_sid:
        return
    description = str(msg.get("description", ""))
    if "64111" not in description and "converting tokens to speech" not in description.lower():
        return
    state = _engine.get_call_state(call_sid)
    if state is None:
        return

    count = int(state.metadata.get("cr_tts_errors", 0)) + 1
    state.metadata["cr_tts_errors"] = count
    if count < MAX_CR_TTS_ERRORS:
        return

    from pincer.voice.language import voice_for
    from pincer.voice.voices import mark_voice_invalid

    bad_voice = voice_for(_settings, state.language) if _settings else ""
    if bad_voice:
        mark_voice_invalid(bad_voice, _settings)
        logger.error(
            "ConversationRelay TTS failing repeatedly [%s] — voice %s marked unusable; "
            "next calls use Twilio's default ElevenLabs voice for the language",
            call_sid,
            bad_voice,
        )
    await _engine.fallback_and_end(call_sid)


@twilio_router.post("/relay-webhook")
async def relay_webhook(request: Request) -> Response:
    """ConversationRelay text webhook — receives transcribed text, returns agent response."""
    if await _authenticate(request) is None:
        return _forbidden()

    if not _engine:
        return PlainTextResponse("Engine not initialized", status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    call_sid = str(body.get("CallSid", body.get("callSid", "")))
    event_type = str(body.get("type", ""))
    _mark_relay_webhook(call_sid)

    if event_type == "prompt":
        text = str(body.get("voicePrompt", ""))
        if text and call_sid:
            await _engine.on_speech_input(call_sid, text)

    elif event_type == "setup":
        caller = str(body.get("from", body.get("From", "")))
        if call_sid and not _engine.get_call_state(call_sid):
            # Same resolver as the relay socket, so the briefing rules cannot
            # differ between the two transports.
            await _resolve_setup_state(
                call_sid,
                caller,
                str(body.get("direction", "")),
                client=client_ip(request),
                to=str(body.get("to", body.get("To", ""))),
                verified=_was_verified(request),
            )
        if call_sid:
            _mark_relay_webhook(call_sid)  # registered just now
            _note_reconnected(_engine.get_call_state(call_sid))
            await _engine.mark_call_answered(call_sid)

    elif event_type == "interrupt":
        # The same barge-in as the relay socket: cancel the turn still writing
        # tokens and re-mark what the caller never heard, not just the audio.
        if call_sid:
            heard = body.get("utteranceUntilInterrupt")
            await _engine.on_caller_barge_in(call_sid, str(heard) if heard is not None else None)

    elif event_type == "error":
        logger.error("ConversationRelay error: %s", body)

    return PlainTextResponse("OK")


@twilio_router.websocket("/stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str) -> None:
    """Media Streams WebSocket endpoint — bidirectional raw audio."""
    if not await _authenticate_ws(websocket, "twilio_ws:stream", WS_STREAM_PATH):
        return

    await websocket.accept()
    logger.info("Media stream connected: %s", call_sid)

    if not _engine:
        await websocket.close(code=1011, reason="Engine not initialized")
        return

    state = _engine.get_call_state(call_sid)
    if state is None:
        # Media Streams: the audio socket can connect before any other webhook
        # has registered the call. The stream carries no direction field, so
        # the resolver falls through to the status record (which knows the
        # briefing); an unknown SID is refused — only /webhook and the dialer
        # may create call state.
        state = await _resolve_setup_state(call_sid, "", "", client=client_ip(websocket))
    if state is None:
        await websocket.close(code=1008, reason="Unknown call")
        return
    existing_ws = state.metadata.get("websocket")
    if (
        existing_ws is not None
        and existing_ws is not websocket
        and getattr(existing_ws, "client_state", None) == WebSocketState.CONNECTED
    ):
        # A live socket already carries this call's audio — never hand the
        # call to a second socket claiming the same SID.
        await audit_rejection(
            "twilio_ws:stream", client_ip(websocket), "live socket takeover refused", call_sid=call_sid
        )
        await websocket.close(code=1008, reason="Already connected")
        return
    state.metadata["websocket"] = websocket
    from pincer.voice.telemetry import hooks as telemetry

    telemetry.media_open(call_sid, engine="media_streams")
    await _engine.mark_call_answered(call_sid)

    # Inbound frame arrival gaps are the only audio-quality signal this
    # transport exposes: Twilio terminates the RTP leg, so there is no jitter
    # or loss statistic to read — only whether frames kept arriving.
    last_frame_ns = 0

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = msg.get("event", "")

            if event == "connected":
                logger.info("Stream connected event: %s", call_sid)

            elif event == "start":
                stream_sid = msg.get("streamSid", "")
                if state:
                    state.metadata["stream_sid"] = stream_sid
                    # {"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1}
                    state.metadata["media_format"] = (msg.get("start") or {}).get("mediaFormat") or {}
                    # Media Streams: set up STT and transcript consumer
                    if hasattr(_engine, "setup_media_stream_stt"):
                        await _engine.setup_media_stream_stt(call_sid, stream_sid)
                logger.info("Stream started: %s (stream=%s)", call_sid, stream_sid)
                # back from a <Dial> transfer: the call is ours again
                await _engine.end_transfer(call_sid)
                _note_reconnected(state)

            elif event == "media":
                payload = msg.get("media", {}).get("payload", "")
                if payload:
                    now_ns = telemetry.now_ns()
                    if last_frame_ns == 0:
                        telemetry.first_inbound_audio(call_sid)
                    else:
                        gap_ms = (now_ns - last_frame_ns) / 1_000_000.0
                        if gap_ms > MEDIA_GAP_THRESHOLD_MS:
                            telemetry.audio_gap(call_sid, gap_ms)
                    last_frame_ns = now_ns
                    await _engine.on_speech_input(call_sid, payload)

            elif event == "stop":
                logger.info("Stream stopped: %s", call_sid)
                break

    except WebSocketDisconnect:
        logger.info("Media stream disconnected: %s", call_sid)
    except Exception:
        logger.exception("Media stream error: %s", call_sid)
    finally:
        # Only this socket's own media: if a new socket took over the call,
        # closing the call's STT, clearing its entry, or ending the call would
        # cut off (or read as "media gone" for) the caller still streaming, as
        # the relay handler already guards.
        if state is not None and state.metadata.get("websocket") is websocket:
            if _engine and hasattr(_engine, "close_media_stream"):
                await _engine.close_media_stream(call_sid)
            state.metadata.pop("websocket", None)
            state.metadata.pop("stream_sid", None)
            telemetry.media_closed(call_sid, reason="media_socket_closed")
            await _media_closed(call_sid)


# ── Live listen-in: Twilio media fork ingress (Sprint 15) ────────────────────


@twilio_router.websocket("/monitor/{call_sid}")
async def monitor_ws(websocket: WebSocket, call_sid: str) -> None:
    """`<Start><Stream track="both_tracks">` target — the rx-only listen-in fork.

    Twilio streams base64 μ-law frames for both tracks here; they are fanned
    out to dashboard listeners by `MonitorHub` and never persisted. This
    socket is one-way by protocol (a `<Start><Stream>` accepts no audio back),
    so nothing here can speak on the call. It is independent of the
    conversation engine: an error here only ends monitoring, never the call.
    """
    from pincer.voice.monitor import END_CALL_ENDED, get_monitor_hub, listen_in_enabled, parse_monitor_frame

    if not await _authenticate_ws(websocket, "twilio_ws:monitor", WS_MONITOR_PATH):
        return
    if not listen_in_enabled(_settings):
        # Feature off: the TwiML never emits the fork, so a connect here is
        # either stale TwiML or a probe. Refuse before accept.
        await audit_rejection("twilio_ws:monitor", client_ip(websocket), "listen-in disabled")
        await websocket.close(code=1008, reason="Listen-in disabled")
        return

    await websocket.accept()
    hub = get_monitor_hub()
    hub.configure(_settings)
    # Outbound TwiML is built before the SID exists ({CallSid} placeholder);
    # the `start` event carries the real SID and wins over the path.
    sid = "" if call_sid == CALL_SID_PLACEHOLDER else call_sid
    attached = False
    if sid:
        hub.attach_source(sid, websocket)
        attached = True
    logger.info("Listen-in monitor connected: %s", sid or "(sid pending)")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = str(msg.get("event", ""))
            if event == "start":
                start = msg.get("start") or {}
                real_sid = str(start.get("callSid", "") or "")
                stream_sid = str(msg.get("streamSid", "") or start.get("streamSid", "") or "")
                if real_sid and real_sid != sid:
                    if attached and sid:
                        hub.end(sid, END_CALL_ENDED)
                    sid = real_sid
                    attached = False
                if sid and not attached:
                    hub.attach_source(sid, websocket, stream_sid=stream_sid)
                    attached = True
                elif sid and stream_sid:
                    hub.attach_source(sid, websocket, stream_sid=stream_sid)
            elif event == "media":
                parsed = parse_monitor_frame(msg)
                if parsed is not None and sid:
                    _event, track, payload, ts = parsed
                    hub.publish(sid, track, payload, ts)
            elif event == "stop":
                logger.info("Listen-in monitor stopped: %s", sid)
                break
    except WebSocketDisconnect:
        logger.info("Listen-in monitor disconnected: %s", sid)
    except Exception:
        logger.exception("Listen-in monitor error: %s", sid)
    finally:
        if sid:
            hub.end(sid, END_CALL_ENDED)


# ── Deprecated /voice/* aliases ───────────────────────────────────────────────
# These remain fully functional so existing Twilio webhook configs keep working.
# Migrate to /api/apps/twilio/* at your convenience.


@voice_router.get("/health", deprecated=True)
async def voice_health_legacy() -> dict[str, Any]:
    return await voice_health()


@voice_router.post("/webhook", deprecated=True)
async def voice_webhook_legacy(request: Request) -> Response:
    return await voice_webhook(request)


@voice_router.post("/status", deprecated=True)
async def voice_status_legacy(request: Request) -> PlainTextResponse:
    return await voice_status(request)


@voice_router.post("/fallback", deprecated=True)
async def voice_fallback_legacy(request: Request) -> Response:
    return await voice_fallback(request)


@voice_router.post("/relay-webhook", deprecated=True)
async def relay_webhook_legacy(request: Request) -> Response:
    return await relay_webhook(request)


@voice_router.websocket("/relay")
async def relay_ws_legacy(websocket: WebSocket) -> None:
    await relay_ws(websocket)


@voice_router.websocket("/stream/{call_sid}")
async def media_stream_ws_legacy(websocket: WebSocket, call_sid: str) -> None:
    await media_stream_ws(websocket, call_sid)


@voice_router.websocket("/monitor/{call_sid}")
async def monitor_ws_legacy(websocket: WebSocket, call_sid: str) -> None:
    await monitor_ws(websocket, call_sid)
