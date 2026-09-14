"""
Process-wide wiring for telephony telemetry.

One place decides whether telemetry is on, where it writes, and which calls are
sampled — so the instrumentation scattered through the voice code can stay a
one-liner that is safe to call unconditionally.

Sampling is **head-based and per call**: the decision is made once when the
call is registered and applies to the whole call, because half a call's spans
is not a diagnosis. Lifecycle events (registered / answered / ended, with the
failure code) are recorded for *every* call regardless of sampling, so a failed
call is always diagnosable at the coarse grain and the call table is never
missing rows. What sampling drops is turn-level detail.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.voice.telemetry import context as ctxmod
from pincer.voice.telemetry.recorder import TelemetryRecorder, get_recorder, set_recorder
from pincer.voice.telemetry.store import SqliteSink
from pincer.voice.telemetry.tracer import CallTracer, drain_pending

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_tracers: dict[str, CallTracer] = {}
_by_call_id: dict[str, CallTracer] = {}
_db_path: str = ""
_enabled = False
_sample_rate = 1.0
_sink: SqliteSink | None = None

#: Bound on retained tracers. A leak here would hold every call's spans in
#: memory; teardown normally removes them, this is the backstop.
_MAX_TRACERS = 200


def enabled() -> bool:
    return _enabled


def sample_rate() -> float:
    return _sample_rate


async def configure(settings: Any) -> None:
    """Start telemetry for this process. Idempotent."""
    global _db_path, _enabled, _sample_rate, _sink  # noqa: PLW0603

    if not bool(getattr(settings, "telephony_telemetry_enabled", True)):
        _enabled = False
        logger.info("Telephony telemetry disabled by configuration")
        return

    _db_path = str(getattr(settings, "db_path", "") or "")
    if not _db_path:
        _enabled = False
        logger.warning("Telephony telemetry needs a database path — staying disabled")
        return

    _sample_rate = max(0.0, min(1.0, float(getattr(settings, "telephony_telemetry_sample_rate", 1.0) or 1.0)))
    _sink = SqliteSink(_db_path)
    recorder = TelemetryRecorder(
        sink=_sink,
        queue_size=int(getattr(settings, "telephony_telemetry_queue_size", 4096) or 4096),
        batch_size=int(getattr(settings, "telephony_telemetry_batch_size", 256) or 256),
        flush_interval_s=float(getattr(settings, "telephony_telemetry_flush_interval_s", 0.5) or 0.5),
        enabled=True,
    )
    set_recorder(recorder)
    await recorder.start()
    _enabled = True
    logger.info("Telephony telemetry started (sample_rate=%.2f, db=%s)", _sample_rate, _db_path)


async def shutdown() -> None:
    """Flush and stop. Bounded — shutdown never waits on a wedged sink."""
    global _enabled  # noqa: PLW0603
    _enabled = False
    await drain_pending()
    recorder = get_recorder()
    await recorder.stop()
    if _sink is not None:
        await _sink.close()


def should_sample(call_id: str) -> bool:
    """Deterministic per-call decision, so a restart mid-call does not flip it."""
    if _sample_rate >= 1.0:
        return True
    if _sample_rate <= 0.0:
        return False
    digest = hashlib.sha1(call_id.encode(), usedforsecurity=False).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < _sample_rate


def start_call(
    *,
    provider_call_id: str,
    direction: str,
    engine: str = "",
    language: str = "",
    tenant_id: str = "",
    model: str = "",
    transport: str = "",
    codec: str = "",
    sample_rate_hz: int = 0,
    from_number: str = "",
    to_number: str = "",
    **attributes: Any,
) -> CallTracer | None:
    """Register a call and return its tracer. ``None`` when telemetry is off.

    Idempotent on ``provider_call_id``: the inbound webhook, the WebSocket
    setup and a status callback all reach here for the same call.
    """
    if not _enabled:
        return None
    existing = _tracers.get(provider_call_id)
    if existing is not None:
        return existing
    try:
        ctx = ctxmod.register_call(
            provider_call_id=provider_call_id,
            direction=direction,
            engine=engine,
            language=language,
            tenant_id=tenant_id,
            model=model,
            transport=transport,
            codec=codec,
            sample_rate=sample_rate_hz,
            sample_rate_used=_sample_rate,
        )
        sampled = should_sample(ctx.call_id)
        if not sampled:
            ctx = ctxmod.update_call(ctx.call_id, sampled=False) or ctx
        tracer = CallTracer(ctx, db_path=_db_path)
        _register(provider_call_id, tracer)
        from pincer.voice.pii_guard import mask_phone_number

        tracer.registered(
            from_number_masked=mask_phone_number(from_number) if from_number else "",
            to_number_masked=mask_phone_number(to_number) if to_number else "",
            **attributes,
        )
        return tracer
    except Exception:
        logger.debug("telephony telemetry start_call failed", exc_info=True)
        return None


def _register(provider_call_id: str, tracer: CallTracer) -> None:
    if provider_call_id:
        _tracers[provider_call_id] = tracer
    _by_call_id[tracer.ctx.call_id] = tracer
    if len(_tracers) > _MAX_TRACERS:
        oldest = next(iter(_tracers))
        stale = _tracers.pop(oldest, None)
        if stale is not None:
            _by_call_id.pop(stale.ctx.call_id, None)


def rekey(old_id: str, provider_call_id: str) -> CallTracer | None:
    """Bind the real CallSid to a call registered before the dial returned."""
    tracer = _tracers.pop(old_id, None)
    if tracer is None:
        return None
    ctx = ctxmod.attach_provider_call_id(tracer.ctx.call_id, provider_call_id)
    if ctx is not None:
        tracer.ctx = ctx
    _tracers[provider_call_id] = tracer
    return tracer


def tracer_for(provider_call_id: str) -> CallTracer | None:
    return _tracers.get(provider_call_id)


def tracer_by_call_id(call_id: str) -> CallTracer | None:
    return _by_call_id.get(call_id)


def forget(provider_call_id: str) -> None:
    tracer = _tracers.pop(provider_call_id, None)
    if tracer is not None:
        _by_call_id.pop(tracer.ctx.call_id, None)


def active_tracers() -> list[CallTracer]:
    return list(_tracers.values())


def health() -> dict[str, Any]:
    """Exporter health, for `GET /api/telephony/health` and the alert rule."""
    stats = get_recorder().stats()
    return {
        "enabled": _enabled,
        "sample_rate": _sample_rate,
        "active_calls_traced": len(_tracers),
        "export": stats.to_dict(),
        "coverage": stats.coverage,
        "healthy": _enabled and stats.healthy,
    }


def reset_for_tests(*, db_path: str | Path = "", sample: float = 1.0, enabled_: bool = True) -> None:
    global _db_path, _enabled, _sample_rate, _sink  # noqa: PLW0603
    _tracers.clear()
    _by_call_id.clear()
    ctxmod.reset_for_tests()
    _db_path = str(db_path)
    _sample_rate = sample
    _enabled = bool(enabled_ and db_path)
    _sink = SqliteSink(_db_path) if _enabled else None
    set_recorder(TelemetryRecorder(sink=_sink, enabled=_enabled) if _enabled else None)
