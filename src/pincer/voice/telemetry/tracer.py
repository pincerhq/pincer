"""
The instrumentation API the voice code calls.

Design constraints, in priority order:

1. **Never break a call.** Every public method swallows its own exceptions. A
   telemetry bug must degrade the dashboard, never the phone call.
2. **Never block audio.** Events and spans go to the bounded recorder queue
   (synchronous `put_nowait`); the derived per-call and per-turn rows are
   written from background tasks, not awaited on the turn path.
3. **Represent overlap honestly.** Spans are intervals and they overlap; the
   call's lifecycle *states* are recorded separately as events and a status
   column. Nothing forces the streaming pipeline into one linear state machine.

Two objects:

``CallTracer``  one per call, lives as long as the call.
``TurnTracer``  one per caller-utterance → response cycle, derives the turn's
                latencies and its critical path when it finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pincer.voice.telemetry import context as ctxmod
from pincer.voice.telemetry import critical_path as cp
from pincer.voice.telemetry import store
from pincer.voice.telemetry.clock import Stopwatch, duration_ms, mono_ns, project_utc, utc_iso
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.records import TelemetrySpan
from pincer.voice.telemetry.schema import CONVERSATION_RELAY, EventName, SpanName, SpanStatus

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Recorded for EVERY call, sampled or not. These are a handful of rows per
#: call and they are what makes an unsampled failed call still diagnosable:
#: when it started, whether it connected, why it ended. Sampling drops
#: turn-level detail, never the call's own story.
_ALWAYS_RECORD: frozenset[str] = frozenset(
    {
        str(EventName.CALL_REGISTERED),
        str(EventName.INBOUND_WEBHOOK),
        str(EventName.DIAL_REQUESTED),
        str(EventName.DIAL_ACCEPTED),
        str(EventName.DIAL_REJECTED),
        str(EventName.PROVIDER_STATUS),
        str(EventName.AMD_VERDICT),
        str(EventName.CALL_ANSWERED),
        str(EventName.MEDIA_STREAM_OPEN),
        str(EventName.MEDIA_STREAM_CLOSED),
        str(EventName.CALL_ENDED),
        str(EventName.CALL_DECLINED),
        str(EventName.CALL_PHASE),
        str(EventName.ERROR),
        str(EventName.TIMEOUT),
        str(EventName.RECONNECT),
    }
)

#: Background persistence tasks, kept referenced so they are not garbage
#: collected mid-flight, and bounded so a stuck database cannot accumulate
#: tasks without limit.
_pending: set[asyncio.Task[Any]] = set()
_MAX_PENDING = 512


def _spawn(coro: Any) -> None:
    """Run a persistence coroutine off the caller's path, or drop it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    if len(_pending) >= _MAX_PENDING:
        coro.close()
        logger.debug("telemetry persistence backlog full — dropping a write")
        return
    task = loop.create_task(_guard(coro))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _guard(coro: Any) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("telemetry persistence failed", exc_info=True)


async def drain_pending(timeout_s: float = 5.0) -> None:
    """Wait for outstanding persistence tasks. Shutdown and tests only."""
    if not _pending:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.gather(*list(_pending), return_exceptions=True), timeout=timeout_s)


@dataclass(slots=True)
class SpanHandle:
    """An open interval. Closing it emits the span record."""

    tracer: CallTracer
    span_id: str
    name: str
    start_ns: int
    parent_span_id: str = ""
    turn_id: str = ""
    attempt: int = 1
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = SpanStatus.OK
    closed: bool = False

    def set(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def close(self, status: str = "", **attributes: Any) -> TelemetrySpan | None:
        if self.closed:
            return None
        self.closed = True
        if attributes:
            self.attributes.update(attributes)
        if status:
            self.status = status
        end_ns = mono_ns()
        record = TelemetrySpan(
            span_id=self.span_id,
            call_id=self.tracer.ctx.call_id,
            trace_id=self.tracer.ctx.trace_id,
            parent_span_id=self.parent_span_id,
            turn_id=self.turn_id,
            name=self.name,
            start_utc=project_utc(self.start_ns).isoformat(),
            end_utc=project_utc(end_ns).isoformat(),
            start_mono_ns=self.start_ns,
            end_mono_ns=end_ns,
            duration_ms=duration_ms(self.start_ns, end_ns),
            status=self.status,
            attempt=self.attempt,
            attributes=dict(self.attributes),
        )
        self.tracer._record_span(record)
        return record


class CallTracer:
    """Telemetry for one call. Cheap to construct, safe to use from any task."""

    def __init__(
        self,
        ctx: ctxmod.CallContext,
        *,
        db_path: str | Path,
        recorder: Any = None,
    ) -> None:
        self.ctx = ctx
        self._db_path = str(db_path)
        self._recorder = recorder or get_recorder()
        self.watch = Stopwatch.start()
        self.span_id = ctxmod.new_span_id()
        self._spans: list[TelemetrySpan] = []
        self._open: dict[str, SpanHandle] = {}
        self._turn_no = 0
        self._counters: dict[str, int] = {}
        self._answered_ns: int | None = None
        self._dialed_ns: int | None = None
        self._media_open_ns: int | None = None
        #: First inbound media frame seen. Guards a per-frame hook that is
        #: called on the audio path — it must stay a single attribute read.
        self.first_inbound_audio_seen = False
        self._finished = False
        # Call-row writes are CHAINED, not merely spawned. `answered()` fires a
        # background write and `finish()` fires the final one; unordered tasks
        # let the earlier write land last and resurrect a stale status on a
        # call that had already ended.
        self._write_chain: asyncio.Task[None] | None = None

    # ── plumbing ─────────────────────────────────────────────────────

    def _record_span(self, record: TelemetrySpan) -> None:
        # Spans are pure detail, so an unsampled call records none. They are
        # still kept in memory for this call's critical-path attribution, which
        # costs nothing and keeps the derivation identical either way.
        self._spans.append(record)
        if not self.ctx.sampled:
            return
        if len(self._spans) > 4000:  # a pathological call must not grow forever
            del self._spans[:1000]
        self._recorder.span(record)

    def _enqueue(self, coro: Any) -> None:
        """Run a call-row write after every write queued before it.

        Still off the caller's path — the chain is what orders them, not the
        caller waiting.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        previous = self._write_chain

        async def _chained() -> None:
            if previous is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await previous
            await _guard(coro)

        task = loop.create_task(_chained())
        self._write_chain = task
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def _await_writes(self, timeout_s: float = 5.0) -> None:
        chain = self._write_chain
        if chain is None:
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(chain), timeout=timeout_s)

    def _bump(self, counter: str, amount: int = 1) -> None:
        self._counters[counter] = self._counters.get(counter, 0) + amount

    def spans_for(self, turn_id: str) -> list[TelemetrySpan]:
        return [s for s in self._spans if s.turn_id == turn_id]

    # ── events ───────────────────────────────────────────────────────

    def event(
        self,
        name: str | EventName,
        *,
        at_ns: int | None = None,
        event_id: str = "",
        turn_id: str = "",
        span_id: str = "",
        **attributes: Any,
    ) -> None:
        try:
            if not self.ctx.sampled and str(name) not in _ALWAYS_RECORD:
                return
            self._recorder.event(
                str(name),
                call=self.ctx,
                at_ns=at_ns,
                event_id=event_id,
                turn_id=turn_id,
                span_id=span_id or self.span_id,
                attributes=attributes,
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("telemetry event failed", exc_info=True)

    def error(self, code: str, *, stage: str = "", fatal: bool = False, **attributes: Any) -> None:
        self._bump("error_count")
        self.event(EventName.ERROR, code=code, stage=stage, fatal=fatal, **attributes)

    def timeout(self, stage: str, *, limit_s: float | None = None, **attributes: Any) -> None:
        self._bump("timeout_count")
        self.event(EventName.TIMEOUT, stage=stage, limit_s=limit_s, **attributes)

    def reconnect(self, component: str, *, attempt: int = 1, **attributes: Any) -> None:
        self._bump("reconnect_count")
        self.event(EventName.RECONNECT, component=component, attempt=attempt, **attributes)

    def retry(self, component: str, *, attempt: int = 2, **attributes: Any) -> None:
        self._bump("retry_count")
        self.event(EventName.ERROR, code=f"{component}_retry", stage=component, attempt=attempt, **attributes)

    def interruption(self, **attributes: Any) -> None:
        self._bump("interruption_count")
        self.event(EventName.BARGE_IN_DETECTED, **attributes)

    # ── spans ────────────────────────────────────────────────────────

    def open_span(
        self,
        name: str | SpanName,
        *,
        parent_span_id: str = "",
        turn_id: str = "",
        attempt: int = 1,
        key: str = "",
        **attributes: Any,
    ) -> SpanHandle:
        handle = SpanHandle(
            tracer=self,
            span_id=ctxmod.new_span_id(),
            name=str(name),
            start_ns=mono_ns(),
            parent_span_id=parent_span_id or self.span_id,
            turn_id=turn_id,
            attempt=attempt,
            attributes=dict(attributes),
        )
        if key:
            self._open[key] = handle
        return handle

    def close_span(self, key: str, status: str = "", **attributes: Any) -> None:
        handle = self._open.pop(key, None)
        if handle is not None:
            handle.close(status, **attributes)

    @contextmanager
    def span(self, name: str | SpanName, **kwargs: Any) -> Iterator[SpanHandle]:
        handle = self.open_span(name, **kwargs)
        try:
            yield handle
        except asyncio.CancelledError:
            handle.close(SpanStatus.CANCELLED)
            raise
        except Exception as e:
            handle.close(SpanStatus.ERROR, error=type(e).__name__)
            raise
        else:
            handle.close()

    # ── lifecycle ────────────────────────────────────────────────────

    def registered(self, **attributes: Any) -> None:
        self.event(EventName.CALL_REGISTERED, **attributes)
        self._enqueue(
            store.upsert_call(
                self._db_path,
                self.ctx.call_id,
                provider_call_id=self.ctx.provider_call_id,
                trace_id=self.ctx.trace_id,
                direction=self.ctx.direction,
                provider=self.ctx.provider,
                engine=self.ctx.engine,
                transport=self.ctx.transport,
                codec=self.ctx.codec,
                sample_rate=self.ctx.sample_rate,
                model=self.ctx.model,
                language=self.ctx.language,
                tenant_id=self.ctx.tenant_id,
                environment=self.ctx.environment,
                app_version=self.ctx.app_version,
                registered_at=self.watch.started_utc.isoformat(),
                status="active",
                sampled=1 if self.ctx.sampled else 0,
                sample_rate_used=self.ctx.sample_rate_used,
                updated_at=utc_iso(),
                **{k: v for k, v in attributes.items() if k in store.CALL_FIELDS},
            )
        )

    def dial_requested(self, **attributes: Any) -> None:
        self._dialed_ns = mono_ns()
        self.event(EventName.DIAL_REQUESTED, at_ns=self._dialed_ns, **attributes)
        self._enqueue(store.upsert_call(self._db_path, self.ctx.call_id, dialed_at=utc_iso(), updated_at=utc_iso()))

    def answered(self, **attributes: Any) -> None:
        """The callee picked up. Registration is NOT an answer — see
        `VoiceEngine.mark_call_answered`; the setup latency depends on it."""
        if self._answered_ns is not None:
            return
        self._answered_ns = mono_ns()
        self.event(EventName.CALL_ANSWERED, at_ns=self._answered_ns, **attributes)
        setup = duration_ms(self._dialed_ns, self._answered_ns) if self._dialed_ns else None
        self._enqueue(
            store.upsert_call(
                self._db_path,
                self.ctx.call_id,
                answered_at=project_utc(self._answered_ns).isoformat(),
                setup_ms=setup,
                status="connected",
                updated_at=utc_iso(),
            )
        )

    def media_open(self, **attributes: Any) -> None:
        if self._media_open_ns is None:
            self._media_open_ns = mono_ns()
        self.event(EventName.MEDIA_STREAM_OPEN, at_ns=self._media_open_ns, **attributes)
        establish = duration_ms(self._answered_ns, self._media_open_ns) if self._answered_ns else None
        self._enqueue(
            store.upsert_call(
                self._db_path,
                self.ctx.call_id,
                media_open_at=project_utc(self._media_open_ns).isoformat(),
                media_establish_ms=establish,
                updated_at=utc_iso(),
            )
        )

    def provider_status(self, status: str, *, sequence: str = "", **attributes: Any) -> None:
        """A provider status callback.

        Deterministic id: Twilio retries these for minutes and the same status
        must not appear twice on the timeline.
        """
        from pincer.voice.telemetry.records import deterministic_event_id

        self.event(
            EventName.PROVIDER_STATUS,
            event_id=deterministic_event_id(self.ctx.call_id, str(EventName.PROVIDER_STATUS), sequence or status),
            status=status,
            **attributes,
        )

    async def finish(
        self,
        *,
        status: str = "ended",
        outcome: str = "",
        failure_code: str = "",
        termination_reason: str = "",
        duration_s: float | None = None,
        **attributes: Any,
    ) -> None:
        """Close the call row. Awaited: this is teardown, not the audio path."""
        if self._finished:
            return
        self._finished = True
        from pincer.voice.telemetry.outcomes import categorise

        for key, handle in list(self._open.items()):
            handle.close(SpanStatus.CANCELLED, reason="call_ended")
            self._open.pop(key, None)

        self.event(
            EventName.CALL_ENDED,
            status=status,
            failure_code=failure_code,
            termination_reason=termination_reason,
            **attributes,
        )
        fields: dict[str, Any] = {
            "status": status,
            "outcome": outcome or status,
            "failure_code": failure_code,
            "failure_category": str(categorise(failure_code or None)),
            "termination_reason": termination_reason,
            "ended_at": utc_iso(),
            "duration_ms": (duration_s * 1000.0) if duration_s is not None else self.watch.elapsed_ms(),
            # NOT turn_count: `turn_count` accumulates, and each TurnTracer
            # already added its own 1. Setting it here as well double-counted
            # every call.
            "updated_at": utc_iso(),
            "engine": self.ctx.engine,
            "model": self.ctx.model,
            "language": self.ctx.language,
            "coverage": "full" if self.ctx.sampled else "lifecycle_only",
        }
        # Counters are ADDED by the upsert, so send only what this tracer saw.
        fields.update({k: v for k, v in self._counters.items() if k in store.CALL_FIELDS})
        # Everything queued earlier must land BEFORE the terminal row, or a
        # late `answered()` write would put an ended call back to "connected".
        await self._await_writes()
        try:
            await store.upsert_call(self._db_path, self.ctx.call_id, **fields)
        except Exception:
            logger.debug("telemetry call finalisation failed", exc_info=True)
        ctxmod.end_call(provider_call_id=self.ctx.provider_call_id, call_id=self.ctx.call_id)

    # ── turns ────────────────────────────────────────────────────────

    def start_turn(self, *, trigger: str = "caller_speech", speech_end_ns: int | None = None) -> TurnTracer:
        self._turn_no += 1
        turn_ctx = ctxmod.start_turn(self._turn_no, trigger=trigger)
        return TurnTracer(self, turn_ctx, speech_end_ns=speech_end_ns)


class TurnTracer:
    """One caller-utterance → response cycle.

    Owns the turn span, the stage stamps, and the derivation of the turn's
    latencies. The turn is closed exactly once — by `finish`, `cancel` or
    `fail` — and writes its row from a background task.
    """

    def __init__(self, call: CallTracer, turn: ctxmod.TurnContext, *, speech_end_ns: int | None = None) -> None:
        self.call = call
        self.turn = turn
        self.started_ns = mono_ns()
        #: Where the response-latency clock starts. The caller's measured
        #: speech end when the engine can observe it; otherwise the moment the
        #: transcript reached us, which is LATER than reality by the
        #: endpointing wait — recorded in `latency_source` so the number is
        #: never mistaken for the real thing.
        self.origin_ns = speech_end_ns if speech_end_ns is not None else self.started_ns
        self.latency_source = "speech_end" if speech_end_ns is not None else "transcript_arrival"
        self.stamps: dict[str, int] = {}
        self.counters: dict[str, int] = {}
        self.first_audio_ns: int | None = None
        self.error: str = ""
        self.cancelled = False
        self.interrupted = False
        self._closed = False
        self._span = call.open_span(SpanName.TURN, turn_id=turn.turn_id, turn_no=turn.turn_no)
        # The turn span's own start is the origin, so the waterfall lines up
        # with the latency the turn reports.
        self._span.start_ns = self.origin_ns
        self.span_id = self._span.span_id
        call.event(
            EventName.TURN_START,
            at_ns=self.started_ns,
            turn_id=turn.turn_id,
            span_id=self.span_id,
            turn_no=turn.turn_no,
            trigger=turn.trigger,
            latency_source=self.latency_source,
        )

    # ── stamping ─────────────────────────────────────────────────────

    def stamp(self, name: str | EventName, *, at_ns: int | None = None, **attributes: Any) -> int:
        """Record a stage boundary once. Repeats are ignored (first wins)."""
        key = str(name)
        when = at_ns if at_ns is not None else mono_ns()
        if key not in self.stamps:
            self.stamps[key] = when
            self.call.event(key, at_ns=when, turn_id=self.turn.turn_id, span_id=self.span_id, **attributes)
        return when

    def bump(self, counter: str, amount: int = 1) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + amount

    def open_span(self, name: str | SpanName, **kwargs: Any) -> SpanHandle:
        kwargs.setdefault("parent_span_id", self.span_id)
        return self.call.open_span(name, turn_id=self.turn.turn_id, **kwargs)

    def first_audio(self, *, kind: str = "audio", at_ns: int | None = None, **attributes: Any) -> None:
        """First response audio (or, on ConversationRelay, first text token)
        handed to the provider. This is SENT, not heard."""
        if self.first_audio_ns is not None:
            return
        self.first_audio_ns = at_ns if at_ns is not None else mono_ns()
        self.call.event(
            EventName.AUDIO_DISPATCHED,
            at_ns=self.first_audio_ns,
            turn_id=self.turn.turn_id,
            span_id=self.span_id,
            kind=kind,
            **attributes,
        )

    # ── closing ──────────────────────────────────────────────────────

    def cancel(self, reason: str = "barge_in") -> None:
        if self._closed:
            return
        self.cancelled = True
        self.interrupted = reason == "barge_in"
        self.call.event(
            EventName.TURN_CANCELLED,
            turn_id=self.turn.turn_id,
            span_id=self.span_id,
            reason=reason,
        )
        self._close(SpanStatus.CANCELLED)

    def fail(self, error: str) -> None:
        if self._closed:
            return
        self.error = error
        self.call.error(error, stage="turn")
        self._close(SpanStatus.ERROR)

    def finish(self) -> None:
        if self._closed:
            return
        self._close(SpanStatus.OK)

    def _close(self, status: str) -> None:
        self._closed = True
        self._span.close(status, turn_no=self.turn.turn_no)
        # Derived HERE, not inside the background write: the row must describe
        # the turn as it was when it ended. Deriving lazily let a later change
        # to the call's dimensions (the engine learned at teardown) rewrite a
        # turn that had already finished.
        row = self.derive()
        self.call.event(
            EventName.TURN_END,
            turn_id=self.turn.turn_id,
            span_id=self.span_id,
            status=status,
            turn_no=self.turn.turn_no,
        )
        self.call._enqueue(self._persist(row))  # noqa: SLF001

    # ── derivation ───────────────────────────────────────────────────

    def derive(self) -> dict[str, Any]:
        """Turn stamps and spans into the row the dashboard reads.

        Every value is a monotonic difference inside this process, and every
        one may be ``None`` — a stage that did not happen (a turn handled
        deterministically never calls the LLM) is absent, not zero.
        """
        s = self.stamps

        def between(a: str | EventName, b: str | EventName) -> float | None:
            return duration_ms(s.get(str(a)), s.get(str(b)))

        response_latency = duration_ms(self.origin_ns, self.first_audio_ns)
        path = self.critical_path()
        tool_total = sum(
            (sp.duration_ms or 0.0) for sp in self.call.spans_for(self.turn.turn_id) if sp.name == SpanName.TOOL
        )

        return {
            "call_id": self.call.ctx.call_id,
            "turn_no": self.turn.turn_no,
            "trigger": self.turn.trigger,
            "started_at": project_utc(self.origin_ns).isoformat(),
            "first_audio_at": (project_utc(self.first_audio_ns).isoformat() if self.first_audio_ns else None),
            "engine": self.call.ctx.engine,
            "model": self.call.ctx.model,
            "language": self.call.ctx.language,
            "response_latency_ms": response_latency,
            "response_latency_source": self.latency_source,
            "endpointing_ms": between(EventName.STT_SPEECH_END, EventName.ENDPOINT_DECISION),
            "stt_first_partial_ms": between(EventName.STT_SPEECH_START, EventName.STT_PARTIAL),
            "stt_final_ms": between(EventName.STT_SPEECH_END, EventName.STT_FINAL),
            "agent_queue_ms": between(EventName.STT_FINAL, EventName.TURN_START)
            or duration_ms(self.origin_ns, self.started_ns),
            "agent_prep_ms": between(EventName.TURN_START, EventName.AGENT_PREP_DONE),
            "llm_ttft_ms": between(EventName.LLM_REQUEST, EventName.LLM_FIRST_TOKEN),
            "llm_total_ms": between(EventName.LLM_REQUEST, EventName.LLM_DONE),
            "tool_total_ms": tool_total or None,
            "tts_first_audio_ms": between(EventName.TTS_REQUEST, EventName.TTS_FIRST_AUDIO),
            "tts_total_ms": between(EventName.TTS_REQUEST, EventName.TTS_DONE),
            "audio_queue_ms": between(EventName.AUDIO_QUEUED, EventName.AUDIO_DISPATCHED),
            "total_ms": duration_ms(self.origin_ns, mono_ns()),
            "tool_calls": self.counters.get("tool_calls", 0),
            "tool_retries": self.counters.get("tool_retries", 0),
            "tool_timeouts": self.counters.get("tool_timeouts", 0),
            "interrupted": 1 if self.interrupted else 0,
            "cancelled": 1 if self.cancelled else 0,
            "error": self.error,
            "bottleneck_stage": path.bottleneck_stage if path else "",
            "bottleneck_ms": path.bottleneck_ms if path else None,
            "critical_path": _dumps(path.to_dict() if path else {}),
            "complete": 1 if (response_latency is not None and not self.error) else 0,
            "created_at": project_utc(self.started_ns).isoformat(),
        }

    def critical_path(self) -> cp.CriticalPath | None:
        """Attribution of the window [origin, first audio] across stages."""
        if self.first_audio_ns is None:
            return None
        spans = [
            cp.PathSpan(
                name=sp.name,
                start_ns=sp.start_mono_ns,
                end_ns=sp.end_mono_ns,
                span_id=sp.span_id,
                status=sp.status,
                attempt=sp.attempt,
                label=str(sp.attributes.get("tool") or sp.attributes.get("label") or ""),
            )
            for sp in self.call.spans_for(self.turn.turn_id)
            if sp.name != SpanName.TURN
        ]
        return cp.compute(origin_ns=self.origin_ns, target_ns=self.first_audio_ns, spans=spans)

    async def _persist(self, row: dict[str, Any]) -> None:
        if self.call.ctx.sampled:
            row = {**row, "streamed": 1}
            await store.save_turn(self.call._db_path, self.turn.turn_id, **row)  # noqa: SLF001
        await store.upsert_call(
            self.call._db_path,  # noqa: SLF001
            self.call.ctx.call_id,
            turn_count=1,
            tool_count=self.counters.get("tool_calls", 0),
            updated_at=utc_iso(),
        )


def _dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def relay_engine(engine: str) -> bool:
    return str(engine or "") == CONVERSATION_RELAY


# ── ambient turn tracer ──────────────────────────────────────────────
#
# The LLM call and the tool executions happen several layers down
# (`Agent.stream_voice_turn`, `InCallToolGate`), and threading a tracer through
# those signatures would couple the agent to the voice subsystem. A contextvar
# keeps the coupling one-way and survives `asyncio.create_task`, which matters
# because the streaming turn runs in its own task.

import contextvars  # noqa: E402

_turn_tracer_var: contextvars.ContextVar[TurnTracer | None] = contextvars.ContextVar("pincer_turn_tracer", default=None)


@contextmanager
def bind_turn_tracer(tracer: TurnTracer | None) -> Iterator[TurnTracer | None]:
    token = _turn_tracer_var.set(tracer)
    try:
        yield tracer
    finally:
        _turn_tracer_var.reset(token)


def current_turn_tracer() -> TurnTracer | None:
    """The turn being traced on this task, if any. ``None`` outside a call."""
    return _turn_tracer_var.get()


def stamp_current(name: str, **attributes: Any) -> None:
    """Stamp a stage boundary on the ambient turn. Safe when there is none."""
    tracer = _turn_tracer_var.get()
    if tracer is None:
        return
    try:
        tracer.stamp(name, **attributes)
    except Exception:  # pragma: no cover - defensive
        logger.debug("turn stamp failed", exc_info=True)
