"""
Exception-proof shims the voice code calls.

Every function here is safe to call unconditionally: telemetry off, tables
missing, no event loop, unknown call — all of them return quietly. That is the
point. Instrumentation that a caller has to guard with `if enabled and tracer
and ...` gets skipped on the error paths, which are exactly the paths that need
it most.

Nothing in here awaits. The two functions that must finish before the process
forgets a call (`call_ended`) are async and are awaited from teardown, which is
not the audio path.
"""

from __future__ import annotations

import logging
from typing import Any

from pincer.voice.telemetry import runtime
from pincer.voice.telemetry.clock import mono_ns
from pincer.voice.telemetry.schema import CONVERSATION_RELAY, MEDIA_STREAMS, EventName

logger = logging.getLogger(__name__)

#: Transport facts per engine. Recorded on the call row so a call details page
#: can state the codec and sample rate instead of leaving an engineer to infer
#: them from the engine name.
TRANSPORT: dict[str, dict[str, Any]] = {
    CONVERSATION_RELAY: {
        "transport": "websocket/conversation_relay (text)",
        "codec": "provider-managed",
        "sample_rate_hz": 0,
    },
    MEDIA_STREAMS: {
        "transport": "websocket/media_streams (audio)",
        "codec": "audio/x-mulaw",
        "sample_rate_hz": 8000,
    },
}


def transport_facts(engine: str) -> dict[str, Any]:
    return dict(TRANSPORT.get(str(engine or ""), {"transport": "", "codec": "", "sample_rate_hz": 0}))


def _safe(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.debug("telephony telemetry hook failed", exc_info=True)
        return None


# ── call lifecycle ───────────────────────────────────────────────────


def call_started(
    call_sid: str,
    *,
    direction: str,
    engine: str = "",
    language: str = "",
    from_number: str = "",
    to_number: str = "",
    tenant_id: str = "",
    **attributes: Any,
) -> Any:
    """Register a call (inbound webhook or outbound pre-dial)."""
    return _safe(
        runtime.start_call,
        provider_call_id=call_sid,
        direction=direction,
        engine=engine,
        language=language,
        tenant_id=tenant_id,
        from_number=from_number,
        to_number=to_number,
        **transport_facts(engine),
        **attributes,
    )


def inbound_webhook(call_sid: str, *, from_number: str, to_number: str, engine: str, language: str) -> Any:
    tracer = call_started(
        call_sid,
        direction="inbound",
        engine=engine,
        language=language,
        from_number=from_number,
        to_number=to_number,
    )
    if tracer is not None:
        _safe(tracer.event, EventName.INBOUND_WEBHOOK, engine=engine, language=language)
    return tracer


async def call_declined(call_sid: str, *, failure_code: str, reason: str = "", language: str = "") -> None:
    """An inbound call refused before it ever reached the engine.

    Terminal immediately: the call never reaches `_handle_call_end`, so nothing
    else would ever close its row. A blocklist or capacity decline is the system
    working, and `categorise()` keeps it out of the technical failure rate.
    """
    tracer = runtime.tracer_for(call_sid)
    if tracer is None:
        tracer = call_started(call_sid, direction="inbound", language=language)
    if tracer is None:
        return
    _safe(tracer.event, EventName.CALL_DECLINED, failure_code=failure_code, reason=reason)
    try:
        await tracer.finish(
            status="failed",
            outcome="declined",
            failure_code=failure_code,
            termination_reason=reason,
            duration_s=0.0,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("declined-call finalisation failed", exc_info=True)
    finally:
        runtime.forget(call_sid)


def dial_requested(call_sid_or_temp: str, *, to_number: str, engine: str, language: str, tenant_id: str = "") -> Any:
    tracer = call_started(
        call_sid_or_temp,
        direction="outbound",
        engine=engine,
        language=language,
        to_number=to_number,
        tenant_id=tenant_id,
    )
    if tracer is not None:
        _safe(tracer.dial_requested, to=to_number)
    return tracer


def dial_accepted(temp_id: str, call_sid: str) -> Any:
    """Twilio named the call. Re-key the tracer onto the real CallSid."""
    tracer = _safe(runtime.rekey, temp_id, call_sid)
    if tracer is not None:
        _safe(tracer.event, EventName.DIAL_ACCEPTED, provider_call_id=call_sid)
    return tracer


def dial_rejected(temp_id: str, *, error: str) -> None:
    tracer = runtime.tracer_for(temp_id)
    if tracer is None:
        return
    _safe(tracer.event, EventName.DIAL_REJECTED, error=error)


def media_open(call_sid: str, *, engine: str = "", stream_sid: str = "") -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is None:
        return
    facts = transport_facts(engine or tracer.ctx.engine)
    _safe(
        tracer.media_open,
        transport=facts["transport"],
        codec=facts["codec"],
        sample_rate_hz=facts["sample_rate_hz"],
        stream_sid=stream_sid,
    )


def call_answered(call_sid: str, **attributes: Any) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.answered, **attributes)


def first_inbound_audio(call_sid: str) -> None:
    """First media frame. Media Streams only — see the schema's availability."""
    tracer = runtime.tracer_for(call_sid)
    if tracer is None or tracer.first_inbound_audio_seen:
        return
    tracer.first_inbound_audio_seen = True
    _safe(tracer.event, EventName.FIRST_INBOUND_AUDIO)


def audio_gap(call_sid: str, gap_ms: float) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.event, EventName.AUDIO_GAP, gap_ms=round(gap_ms, 1))


def interruption_reported(call_sid: str, *, engine: str = "", **attributes: Any) -> None:
    """Barge-in the provider detected and acted on before telling us.

    Counted, but it yields no interruption LATENCY: we did not perform the
    cancellation, so there is no interval of ours to measure.
    """
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.interruption, engine=engine, performed_by="provider", **attributes)


def interruption_detected(call_sid: str, **attributes: Any) -> int:
    """Barge-in WE act on. Returns the monotonic stamp to close against."""
    tracer = runtime.tracer_for(call_sid)
    at = mono_ns()
    if tracer is not None:
        _safe(tracer.interruption, performed_by="pincer", **attributes)
    return at


def buffer_cleared(call_sid: str, *, since_ns: int | None = None, acknowledged: bool = False) -> None:
    """The buffer-clear was written to the provider socket.

    `acknowledged=False` is the truth on Twilio Media Streams: a `clear` is
    fire-and-forget, so this marks when WE stopped sending, not when the caller
    stopped hearing.
    """
    tracer = runtime.tracer_for(call_sid)
    if tracer is None:
        return
    attrs: dict[str, Any] = {"acknowledged": acknowledged}
    if since_ns is not None:
        attrs["interruption_latency_ms"] = round((mono_ns() - since_ns) / 1_000_000.0, 2)
    _safe(tracer.event, EventName.BUFFER_CLEARED, **attrs)


def provider_status(call_sid: str, status: str, **attributes: Any) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.provider_status, status, **attributes)


def amd_verdict(call_sid: str, answered_by: str) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.event, EventName.AMD_VERDICT, answered_by=answered_by)


def media_closed(call_sid: str, *, reason: str = "") -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.event, EventName.MEDIA_STREAM_CLOSED, reason=reason)


def reconnect(call_sid: str, component: str, *, attempt: int = 1) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.reconnect, component, attempt=attempt)


def error(call_sid: str, code: str, *, stage: str = "", **attributes: Any) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.error, code, stage=stage, **attributes)


def timeout(call_sid: str, stage: str, *, limit_s: float | None = None) -> None:
    tracer = runtime.tracer_for(call_sid)
    if tracer is not None:
        _safe(tracer.timeout, stage, limit_s=limit_s)


async def call_ended(
    call_sid: str,
    *,
    status: str,
    failure_code: str = "",
    termination_reason: str = "",
    duration_s: float | None = None,
    engine: str = "",
    language: str = "",
) -> None:
    """Close the call row and release the tracer. Awaited from teardown."""
    tracer = runtime.tracer_for(call_sid)
    if tracer is None:
        return
    try:
        if engine or language:
            from pincer.voice.telemetry import context as ctxmod

            updated = ctxmod.update_call(tracer.ctx.call_id, engine=engine, language=language)
            if updated is not None:
                tracer.ctx = updated
        await tracer.finish(
            status=status,
            failure_code=failure_code,
            termination_reason=termination_reason,
            duration_s=duration_s,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("telephony telemetry call_ended failed", exc_info=True)
    finally:
        runtime.forget(call_sid)


# ── turn helpers ─────────────────────────────────────────────────────


def start_turn(call_sid: str, *, speech_end_ns: int | None = None, trigger: str = "caller_speech") -> Any:
    tracer = runtime.tracer_for(call_sid)
    if tracer is None:
        return None
    return _safe(tracer.start_turn, trigger=trigger, speech_end_ns=speech_end_ns)


def now_ns() -> int:
    return mono_ns()
