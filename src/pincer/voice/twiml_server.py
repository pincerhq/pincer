"""
TwiML server — FastAPI endpoints for Twilio voice webhooks.

Handles inbound call routing, status callbacks, ConversationRelay webhooks,
Media Streams WebSocket connections, and fallback error handling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

twilio_router = APIRouter(prefix="/api/apps/twilio", tags=["apps", "twilio"])
voice_router = APIRouter(prefix="/voice", tags=["voice"])

_engine: VoiceEngine | None = None
_settings: Settings | None = None


def init_voice_routes(engine: VoiceEngine, settings: Settings) -> None:
    """Wire up the voice engine and settings for route handlers."""
    global _engine, _settings  # noqa: PLW0603
    _engine = engine
    _settings = settings


def _validate_twilio_signature(request: Request, body: bytes) -> bool:
    """Validate Twilio webhook HMAC signature to prevent spoofed requests."""
    if not _settings:
        return False
    auth_token = _settings.twilio_auth_token.get_secret_value()
    if not auth_token:
        return True  # no token configured, skip validation

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    url = str(request.url)
    try:
        params = dict(sorted((k, v) for k, v in ((k, request.query_params.get(k, "")) for k in request.query_params)))
        if body:
            from urllib.parse import parse_qs

            form_data = parse_qs(body.decode("utf-8", errors="replace"))
            for k, v in sorted(form_data.items()):
                params[k] = v[0] if v else ""
    except Exception:
        params = {}

    data_str = url + urlencode(sorted(params.items()))
    computed = hmac.new(
        auth_token.encode("utf-8"),
        data_str.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    import base64

    expected = base64.b64encode(computed).decode("utf-8")
    return hmac.compare_digest(expected, signature)


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
    await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND, language=call_language)

    twiml = build_connect_twiml(
        _settings,
        call_sid=call_sid,
        direction="inbound",
        language=call_language,
        counterparty=caller,
    )
    return _twiml_response(twiml)


@twilio_router.post("/status")
async def voice_status(request: Request) -> PlainTextResponse:
    """Call status callbacks (ringing, answered, completed) + AMD results."""
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
            await notify_ended(call_sid, voicemail_reason)
            if _engine:
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
        await notify_ended(call_sid, reasons[user_lang].get(status, status))
        if _engine and _engine.get_call_state(call_sid):
            await _engine.end_call(call_sid)

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
                if state is None:
                    # Outbound calls were placed via REST (voice/outbound.py);
                    # their target, purpose, and language are tracked there.
                    from pincer.voice.engine import CallDirection
                    from pincer.voice.status_notify import get_call_info

                    info = get_call_info(call_sid)
                    if info is not None:
                        state = await _engine.on_call_start(
                            call_sid,
                            info.target_number or caller,
                            CallDirection.OUTBOUND,
                            target_number=info.target_number,
                            purpose=info.purpose,
                            language=info.language,
                        )
                    else:
                        state = await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND)
                state.metadata["websocket"] = websocket

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
            from pincer.voice.engine import CallDirection
            from pincer.voice.status_notify import get_call_info

            # Outbound calls were placed via REST (voice/outbound.py); their
            # target, purpose, and per-call language are tracked there.
            info = get_call_info(call_sid)
            if info is not None:
                await _engine.on_call_start(
                    call_sid,
                    info.target_number or caller,
                    CallDirection.OUTBOUND,
                    target_number=info.target_number,
                    purpose=info.purpose,
                    language=info.language,
                )
            else:
                await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND)

    elif event_type == "interrupt":
        if call_sid:
            await _engine.interrupt_speech(call_sid)

    elif event_type == "error":
        logger.error("ConversationRelay error: %s", body)

    return PlainTextResponse("OK")


@twilio_router.websocket("/stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str) -> None:
    """Media Streams WebSocket endpoint — bidirectional raw audio."""
    await websocket.accept()
    logger.info("Media stream connected: %s", call_sid)

    if not _engine:
        await websocket.close(code=1011, reason="Engine not initialized")
        return

    state = _engine.get_call_state(call_sid)
    if state is None:
        # Outbound media_streams call: the stream may connect before any other
        # webhook has registered the call — recover its context (incl. language)
        from pincer.voice.engine import CallDirection
        from pincer.voice.status_notify import get_call_info

        info = get_call_info(call_sid)
        if info is not None:
            state = await _engine.on_call_start(
                call_sid,
                info.target_number,
                CallDirection.OUTBOUND,
                target_number=info.target_number,
                purpose=info.purpose,
                language=info.language,
            )
    if state:
        state.metadata["websocket"] = websocket

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
