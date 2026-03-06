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

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

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
        params = dict(sorted(
            (k, v) for k, v in
            ((k, request.query_params.get(k, "")) for k in request.query_params)
        ))
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


@voice_router.get("/health")
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


@voice_router.post("/webhook")
async def voice_webhook(request: Request) -> Response:
    """Inbound call handler — returns TwiML to start a stream or ConversationRelay."""
    if not _engine or not _settings:
        return _twiml_response(
            "<Response><Say>Voice system is not configured.</Say><Hangup/></Response>"
        )

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
            return _twiml_response(
                "<Response><Say>This number is not authorized.</Say><Hangup/></Response>"
            )

    from pincer.voice.engine import CallDirection
    await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND)

    base_url = _settings.voice_webhook_base_url.strip().rstrip("/")
    engine_type = _settings.voice_engine.lower().strip()

    if engine_type == "media_streams":
        stream_url = f"wss://{_extract_host(base_url)}/voice/stream/{call_sid}"
        status_url = f"{base_url}/voice/status"
        twiml = (
            "<Response>"
            "<Say>Connecting you now.</Say>"
            f'<Connect><Stream url="{stream_url}" '
            f'statusCallbackUrl="{status_url}" /></Connect>'
            "</Response>"
        )
    else:
        relay_url = f"{base_url}/voice/relay-webhook"
        twiml = (
            "<Response>"
            "<Say>Please wait while I connect you to your assistant.</Say>"
            f'<Connect><ConversationRelay url="{relay_url}" '
            f'voice="Google.en-US-Neural2-F" language="{_settings.voice_language}" '
            'transcriptionProvider="google" ttsProvider="google" /></Connect>'
            "</Response>"
        )

    return _twiml_response(twiml)


@voice_router.post("/status")
async def voice_status(request: Request) -> PlainTextResponse:
    """Call status callbacks (ringing, answered, completed)."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    status = str(form.get("CallStatus", ""))
    duration = form.get("CallDuration", "0")

    logger.info("Call status: %s -> %s (duration=%s)", call_sid, status, duration)

    if status == "completed" and _engine:
        state = _engine.get_call_state(call_sid)
        if state:
            await _engine.end_call(call_sid)

    return PlainTextResponse("OK")


@voice_router.post("/fallback")
async def voice_fallback(request: Request) -> Response:
    """Error fallback — plays apology message, logs error."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    error_code = str(form.get("ErrorCode", ""))
    error_msg = str(form.get("ErrorMessage", ""))

    logger.error(
        "Voice fallback triggered: call=%s code=%s msg=%s",
        call_sid, error_code, error_msg,
    )

    return _twiml_response(
        "<Response>"
        "<Say>I'm sorry, I'm experiencing technical difficulties. "
        "Please try again later.</Say>"
        "<Hangup/>"
        "</Response>"
    )


@voice_router.post("/relay-webhook")
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
            await _engine.on_call_start(call_sid, caller, CallDirection.INBOUND)

    elif event_type == "interrupt":
        if call_sid:
            await _engine.interrupt_speech(call_sid)

    elif event_type == "error":
        logger.error("ConversationRelay error: %s", body)

    return PlainTextResponse("OK")


@voice_router.websocket("/stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str) -> None:
    """Media Streams WebSocket endpoint — bidirectional raw audio."""
    await websocket.accept()
    logger.info("Media stream connected: %s", call_sid)

    if not _engine:
        await websocket.close(code=1011, reason="Engine not initialized")
        return

    state = _engine.get_call_state(call_sid)
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


def _extract_host(base_url: str) -> str:
    """Extract host from a URL (strip scheme)."""
    host = base_url
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.rstrip("/")
