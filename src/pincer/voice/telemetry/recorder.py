"""
Bounded, non-blocking telemetry export.

The hard constraint: **telemetry must never stall audio.** A turn that waits on
a database write is a turn the caller hears as silence, so nothing on the audio
path may await the sink. `emit_event`/`emit_span` are synchronous, they
`put_nowait` onto a bounded queue, and when that queue is full they *drop* and
count the drop. A drop is a visible, reported number — an unbounded queue that
eats memory, or a blocking write, would be worse and quieter.

Export failures are counted the same way and surfaced through
`GET /api/telephony/health`, so "our latency looks great" is never allowed to
mean "we stopped recording".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pincer.voice.telemetry import context as ctx
from pincer.voice.telemetry.clock import mono_ns, project_utc, utc_iso
from pincer.voice.telemetry.records import TelemetryEvent, TelemetrySpan, safe_attributes

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 4096
DEFAULT_BATCH_SIZE = 256
DEFAULT_FLUSH_INTERVAL_S = 0.5
#: Consecutive sink failures after which the recorder stops trying for a while.
#: A sink that is down should cost one failed write per backoff window, not one
#: per event.
_FAILURE_BACKOFF_S = 5.0


class TelemetrySink(Protocol):
    """Anything that can durably accept a batch. Implemented by `SqliteSink`."""

    async def write(self, events: Sequence[TelemetryEvent], spans: Sequence[TelemetrySpan]) -> None: ...


@dataclass
class ExportStats:
    queued: int = 0
    exported: int = 0
    dropped_queue_full: int = 0
    dropped_not_started: int = 0
    export_failures: int = 0
    last_error: str = ""
    last_export_utc: str = ""
    queue_depth: int = 0
    queue_capacity: int = 0
    running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def healthy(self) -> bool:
        return self.export_failures == 0 and self.dropped_queue_full == 0

    @property
    def coverage(self) -> float:
        """Fraction of emitted records that reached the sink.

        1.0 with zero traffic — "nothing recorded because nothing happened" is
        not a coverage problem, and reporting 0.0 there would page someone at
        3am on an idle system.
        """
        if self.queued == 0:
            return 1.0
        return min(1.0, self.exported / float(self.queued))


class TelemetryRecorder:
    """Owns the queue, the writer task and the counters."""

    def __init__(
        self,
        sink: TelemetrySink | None = None,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self._sink = sink
        self._queue: asyncio.Queue[TelemetryEvent | TelemetrySpan] = asyncio.Queue(maxsize=max(1, queue_size))
        self._batch_size = max(1, batch_size)
        self._flush_interval_s = max(0.01, flush_interval_s)
        self._task: asyncio.Task[None] | None = None
        self._stats = ExportStats(queue_capacity=max(1, queue_size))
        self._enabled = enabled
        self._failure_until = 0.0
        # `_flush_now` lets a caller cut the writer's in-flight batch short;
        # `_idle` is set whenever the writer holds nothing. Together they make
        # `flush()` mean "everything emitted so far is durable", which shutdown
        # and tests both need and a plain queue-drain does not give.
        self._flush_now = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled and self._sink is not None

    def set_sink(self, sink: TelemetrySink | None) -> None:
        self._sink = sink

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._writer_loop(), name="telephony-telemetry-export")
        self._stats.running = True

    async def stop(self, *, drain_timeout_s: float = 5.0) -> None:
        """Flush what is queued, then stop. Bounded: shutdown never hangs on a
        sink that has stopped responding."""
        self._stats.running = False
        task = self._task
        self._task = None
        if task is None:
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._drain(), timeout=drain_timeout_s)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _drain(self) -> None:
        await self.flush()

    async def flush(self, *, timeout_s: float = 2.0) -> None:
        """Make everything emitted so far durable.

        Shutdown and tests only — never called from the audio path. Cuts the
        writer's in-flight batch short rather than waiting out its flush
        interval, then writes anything still queued itself (which is what
        happens when no writer task is running at all).
        """
        self._flush_now.set()
        try:
            deadline = time.monotonic() + max(0.0, timeout_s)
            while time.monotonic() < deadline:
                if self._queue.empty() and self._idle.is_set():
                    break
                await asyncio.sleep(0.002)
        finally:
            self._flush_now.clear()
        batch: list[TelemetryEvent | TelemetrySpan] = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if batch:
            await self._write(batch)

    # ── emit (audio path; must not block, must not raise) ────────────

    def _submit(self, item: TelemetryEvent | TelemetrySpan) -> None:
        if not self.enabled:
            self._stats.dropped_not_started += 1
            return
        self._stats.queued += 1
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._stats.dropped_queue_full += 1
            if self._stats.dropped_queue_full == 1:
                logger.warning("Telephony telemetry queue full — records are being dropped")
        self._stats.queue_depth = self._queue.qsize()

    def event(
        self,
        name: str,
        *,
        call: ctx.CallContext | None = None,
        turn_id: str = "",
        span_id: str = "",
        at_ns: int | None = None,
        event_id: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> TelemetryEvent | None:
        """Record a point-in-time fact. Returns the record, or None when it was
        not recorded at all. Never raises.

        The enabled check comes first so a disabled recorder does no work per
        event — that is what keeps instrumentation overhead at zero when
        telemetry is off, rather than merely small.
        """
        if not self.enabled:
            self._stats.dropped_not_started += 1
            return None
        try:
            call_ctx = call or ctx.current_call()
            if call_ctx is None:
                return None
            turn = ctx.current_turn()
            stamp_ns = at_ns if at_ns is not None else mono_ns()
            record = TelemetryEvent(
                event_id=event_id or _random_id(),
                call_id=call_ctx.call_id,
                provider_call_id=call_ctx.provider_call_id,
                trace_id=call_ctx.trace_id,
                span_id=span_id or ctx.current_span_id(),
                turn_id=turn_id or (turn.turn_id if turn else ""),
                name=str(name),
                ts_utc=project_utc(stamp_ns).isoformat() if at_ns is not None else utc_iso(),
                mono_ns=stamp_ns,
                seq=ctx.next_seq(call_ctx.call_id),
                attributes=safe_attributes(attributes),
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("telemetry event build failed", exc_info=True)
            return None
        self._submit(record)
        return record

    def span(self, record: TelemetrySpan) -> TelemetrySpan:
        """Record a completed interval. Never raises."""
        if not self.enabled:
            self._stats.dropped_not_started += 1
            return record
        try:
            record.attributes = safe_attributes(record.attributes)
        except Exception:  # pragma: no cover - defensive
            record.attributes = {}
        self._submit(record)
        return record

    # ── writer ───────────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        while True:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                raise
            self._idle.clear()
            batch: list[TelemetryEvent | TelemetrySpan] = [first]
            deadline = time.monotonic() + self._flush_interval_s
            while len(batch) < self._batch_size:
                if self._flush_now.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
            await self._write(batch)
            self._stats.queue_depth = self._queue.qsize()
            if self._queue.empty():
                self._idle.set()

    async def _write(self, batch: list[TelemetryEvent | TelemetrySpan]) -> None:
        sink = self._sink
        if sink is None:
            self._stats.dropped_not_started += len(batch)
            return
        if time.monotonic() < self._failure_until:
            # Still inside the backoff window from a previous failure. Counting
            # these as export failures too keeps the health number honest: the
            # records are gone either way.
            self._stats.export_failures += len(batch)
            return
        events = [r for r in batch if isinstance(r, TelemetryEvent)]
        spans = [r for r in batch if isinstance(r, TelemetrySpan)]
        try:
            await sink.write(events, spans)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._stats.export_failures += len(batch)
            self._stats.last_error = f"{type(e).__name__}: {e}"[:200]
            self._failure_until = time.monotonic() + _FAILURE_BACKOFF_S
            logger.warning("Telephony telemetry export failed: %s", e)
            return
        self._stats.exported += len(batch)
        self._stats.last_export_utc = utc_iso()

    # ── introspection ────────────────────────────────────────────────

    def stats(self) -> ExportStats:
        self._stats.queue_depth = self._queue.qsize()
        self._stats.running = self._task is not None and not self._task.done()
        return self._stats

    def reset_stats(self) -> None:
        capacity = self._stats.queue_capacity
        self._stats = ExportStats(queue_capacity=capacity)


def _random_id() -> str:
    import secrets

    return secrets.token_hex(16)


@dataclass
class _Global:
    recorder: TelemetryRecorder | None = None
    _lock: Any = field(default=None, repr=False)


_global = _Global()


def get_recorder() -> TelemetryRecorder:
    """The process-wide recorder. Disabled (drops everything, cheaply) until
    `configure` wires a sink — so importing this module never creates a file."""
    if _global.recorder is None:
        _global.recorder = TelemetryRecorder(sink=None, enabled=False)
    return _global.recorder


def set_recorder(recorder: TelemetryRecorder | None) -> None:
    _global.recorder = recorder


def reset_for_tests() -> None:
    _global.recorder = None
