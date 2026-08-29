"""
TwiML server — FastAPI endpoints for Twilio voice webhooks.

Handles inbound call routing, status callbacks, ConversationRelay webhooks,
Media Streams WebSocket connections, and fallback error handling.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response
from starlette.websockets import WebSocketState

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
        await verify_http_request(request, body, _settings)
    except WebhookAuthError as e:
        await audit_rejection(f"twilio_webhook:{request.url.path}", client_ip(request), e.reason)
        return None
    return body


def _forbidden() -> Response:
    return PlainTextResponse("Forbidden", status_code=403)


async def _authenticate_ws(websocket: WebSocket, surface: str, path: str) -> bool:
    """Verify a WS upgrade BEFORE accept(); closes the handshake with 403 on failure."""
    try:
        verify_ws_upgrade(websocket, _settings, path)
    except WebhookAuthError as e:
        await audit_rejection(surface, client_ip(websocket), e.reason)
        await websocket.close(code=1008, reason="Unauthorized")
        return False
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


async def _record_declined_call(call_sid: str, caller: str, failure_code: str, language: str) -> None:
    """Declined calls never reach the engine, so the row + metric are written here."""
    try:
        from pincer.observability.metrics import record_call_ended, record_inbound_event

        record_inbound_event(failure_code, language=language)
        record_call_ended(direction="inbound", outcome="failed", failure_code=failure_code, language=language)
    except Exception:
        logger.debug("declined-call metrics failed", exc_info=True)
    if _settings is None:
        return
    try:
        import aiosqlite

        from pincer.voice.retention import ensure_voice_tables

        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(str(_settings.db_path)) as db:
            await ensure_voice_tables(db)
            await db.execute(
                "INSERT OR REPLACE INTO voice_calls (call_sid, direction, from_number, to_number, started_at, "
                "ended_at, failure_code, engine, language) VALUES (?, 'inbound', ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_sid,
                    caller,
                    str(getattr(_settings, "twilio_phone_number", "") or ""),
                    now,
                    now,
                    failure_code,
                    str(getattr(_settings, "voice_engine", "") or ""),
                    language,
                ),
            )
            await db.commit()
    except Exception:
        logger.debug("declined-call row failed [%s]", call_sid, exc_info=True)


@twilio_router.post("/transfer-result")
async def voice_transfer_result(request: Request) -> Response:
    """Sprint 12 §8.4: <Dial action> callback after a receptionist transfer.

    completed → nothing to do (Twilio hangs up after the bridged leg). Anything
    else (busy / no-answer / failed / canceled) → apology + message-taking:
    the call is reconnected to the relay with the apology as its greeting and
    the session already in TAKE_MESSAGE (name question asked)."""
    if await _authenticate(request) is None:
        return _forbidden()
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    dial_status = str(form.get("DialCallStatus", "")).lower()
    logger.info("Transfer result [%s]: %s", call_sid, dial_status or "-")
    if dial_status == "completed" or not _engine or not _settings:
        return _twiml_response("<Response><Hangup/></Response>")

    from xml.sax.saxutils import escape

    from pincer.voice.twiml_builder import build_connect_twiml

    state = _engine.get_call_state(call_sid)
    session = _transfer_session_lookup(call_sid)
    language = str(getattr(state, "language", "") or "en")
    greeting = ""
    if session is not None:
        greeting = await session.on_transfer_failed()
    twiml = build_connect_twiml(_settings, call_sid=call_sid, direction="inbound", language=language)
    if greeting:
        # Replace the welcome greeting with the apology + first question
        import re as _re

        escaped = escape(greeting, {'"': "&quot;"})
        if "welcomeGreeting=" in twiml:
            twiml = _re.sub(r'welcomeGreeting="[^"]*"', f'welcomeGreeting="{escaped}"', twiml, count=1)
        else:
            twiml = twiml.replace("<ConversationRelay ", f'<ConversationRelay welcomeGreeting="{escaped}" ', 1)
    return _twiml_response(twiml)


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

    from pincer.voice.status_notify import get_call_language

    # Reports reach the user in the language of their initiating command (Sprint 3)
    call_lang = get_call_language(call_sid).strip().lower()[:2]
    user_lang = call_lang if call_lang in ("de", "uk") else "en"

    # T1.3: Answering Machine Detection — voicemail is reported, not conversed with.
    if answered_by.startswith("machine") or answered_by == "fax":
        amd_state = _engine.get_call_state(call_sid) if _engine else None
        if amd_state is not None and amd_state.metadata.get("caller_spoke"):
            # AMD false positive: a human is already mid-conversation. Acting
            # on the verdict here killed live calls 20s in — ignore it and let
            # the status fall through to normal handling.
            logger.info(
                "AMD verdict '%s' ignored [%s] — caller already conversing",
                answered_by,
                call_sid,
            )
        else:
            logger.info("Voicemail/machine detected [%s] (%s) — hanging up", call_sid, answered_by)
            voicemail_reasons = {
                "en": "voicemail detected, no message left",
                "de": "Anrufbeantworter erkannt, keine Nachricht hinterlassen",
                "uk": "виявлено автовідповідач, повідомлення не залишено",
            }
            voicemail_reason = voicemail_reasons[user_lang]
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
                # structured report would be built but never delivered.
                vm_state.metadata["end_reason"] = voicemail_reason
            else:
                # No conversation state -> end_call fires no post-call pipeline
                await notify_ended(call_sid, voicemail_reason)
            if _engine:
                # The call is live (greeting a voicemail box) — always hang up
                await _engine.end_call(call_sid)
            return PlainTextResponse("OK")

    if status == "in-progress":
        # Callee (a human) picked up
        await notify_connected(call_sid)

    elif status in ("busy", "no-answer", "failed", "canceled"):
        # Call never connected — the user still gets a final status (T1.3/T1.5)
        reasons = {
            "en": {
                "busy": "the line was busy",
                "no-answer": "no answer",
                "failed": "the call could not be placed",
                "canceled": "the call was canceled",
            },
            "de": {
                "busy": "die Leitung war besetzt",
                "no-answer": "keine Antwort",
                "failed": "der Anruf konnte nicht aufgebaut werden",
                "canceled": "der Anruf wurde abgebrochen",
            },
            "uk": {
                "busy": "лінія була зайнята",
                "no-answer": "немає відповіді",
                "failed": "не вдалося здійснити дзвінок",
                "canceled": "дзвінок було скасовано",
            },
        }
        not_connected_reason = reasons[user_lang].get(status, status)
        # Appointment calls (Sprint 6): busy/no-answer/failed triggers the retry policy
        if _settings is not None:
            from pincer.voice.scheduling import handle_call_not_connected

            await handle_call_not_connected(call_sid, status, _settings)
        nc_state = _engine.get_call_state(call_sid) if _engine else None
        if _engine is not None and nc_state is not None:
            # Conversation state exists (e.g. a late failure) -> the post-call
            # pipeline sends the final report; pre-empting it here would
            # consume the ENDED stage and drop that report.
            nc_state.metadata["end_reason"] = not_connected_reason
            await _engine.end_call(call_sid)
        else:
            # Usual shape: the call never connected, no pipeline will run
            await notify_ended(call_sid, not_connected_reason)

    elif status == "completed" and _engine:
        state = _engine.get_call_state(call_sid)
        if state:
            await _engine.end_call(call_sid)
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
                state = _engine.get_call_state(call_sid)
                if state is not None:
                    # back from a <Dial> transfer: the call is ours again
                    state.metadata.pop("transferring", None)
                if state is None:
                    state = await _resolve_setup_state(
                        call_sid, caller, str(msg.get("direction", "")), client=client_ip(websocket)
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
                # `setup` is Twilio's "the callee picked up". Every clock in
                # the call starts from here — an outbound state exists from
                # before the dial, so registration is NOT an answer.
                await _engine.mark_call_answered(call_sid)

            elif msg_type == "prompt":
                text = str(msg.get("voicePrompt", ""))
                if text and call_sid:
                    await _engine.on_speech_input(call_sid, text)

            elif msg_type == "interrupt":
                if call_sid:
                    await _engine.interrupt_speech(call_sid)

            elif msg_type == "error":
                logger.error("ConversationRelay error [%s]: %s", call_sid, msg)
                await _handle_relay_error(call_sid, msg)

    except WebSocketDisconnect:
        logger.info("ConversationRelay disconnected: %s", call_sid)
    except Exception:
        logger.exception("ConversationRelay WS error: %s", call_sid)
    finally:
        if state is not None and state.metadata.get("websocket") is websocket:
            state.metadata.pop("websocket", None)
            # socket gone = call over (inbound calls have no other end signal)
            await _media_closed(call_sid)


async def _resolve_setup_state(call_sid: str, caller: str, direction: str = "", client: str = "") -> Any:
    """The call state a `setup` message belongs to, or None to refuse the call.

    Order matters, and each step exists because the previous one can lose a
    briefing:

    1. The state registered before the dial (the normal outbound path).
    2. A short wait — `setup` can arrive before `calls.create()` has even
       returned, so the promotion may be milliseconds away.
    3. The status_notify record, which also carries purpose and language.
    4. Give up. If Twilio told us this is an outbound API call, we return None
       and the caller hangs up: an outbound call whose briefing cannot be
       found MUST NOT run, because what it runs instead is a generic assistant
       persona on a call the user gave a task to.
    5. Otherwise refuse: an unknown SID on a voice socket is never a
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
            import aiosqlite

            from pincer.voice.retention import ensure_voice_tables

            now = datetime.now(UTC).isoformat()
            async with aiosqlite.connect(str(_settings.db_path)) as db:
                await ensure_voice_tables(db)
                await db.execute(
                    "INSERT OR REPLACE INTO voice_calls (call_sid, direction, from_number, to_number, "
                    "started_at, ended_at, failure_code, engine, language) "
                    "VALUES (?, 'outbound', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_sid,
                        str(getattr(_settings, "twilio_phone_number", "") or ""),
                        str(getattr(info, "target_number", "") or "") if info is not None else "",
                        now,
                        now,
                        str(FailureCode.BRIEFING_LOST),
                        str(getattr(_settings, "voice_engine", "") or ""),
                        language,
                    ),
                )
                await db.commit()
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
        mark_voice_invalid(bad_voice)
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

    if event_type == "prompt":
        text = str(body.get("voicePrompt", ""))
        if text and call_sid:
            await _engine.on_speech_input(call_sid, text)

    elif event_type == "setup":
        caller = str(body.get("from", body.get("From", "")))
        if call_sid and not _engine.get_call_state(call_sid):
            # Same resolver as the relay socket, so the briefing rules cannot
            # differ between the two transports.
            await _resolve_setup_state(call_sid, caller, str(body.get("direction", "")), client=client_ip(request))
        if call_sid:
            await _engine.mark_call_answered(call_sid)

    elif event_type == "interrupt":
        if call_sid:
            await _engine.interrupt_speech(call_sid)

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
    await _engine.mark_call_answered(call_sid)

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
                    # Media Streams: set up STT and transcript consumer
                    if hasattr(_engine, "setup_media_stream_stt"):
                        await _engine.setup_media_stream_stt(call_sid, stream_sid)
                logger.info("Stream started: %s (stream=%s)", call_sid, stream_sid)

            elif event == "media":
                payload = msg.get("media", {}).get("payload", "")
                if payload:
                    await _engine.on_speech_input(call_sid, payload)

            elif event == "stop":
                logger.info("Stream stopped: %s", call_sid)
                break

    except WebSocketDisconnect:
        logger.info("Media stream disconnected: %s", call_sid)
    except Exception:
        logger.exception("Media stream error: %s", call_sid)
    finally:
        if _engine and hasattr(_engine, "close_media_stream"):
            await _engine.close_media_stream(call_sid)
        if state:
            state.metadata.pop("websocket", None)
            state.metadata.pop("stream_sid", None)
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
