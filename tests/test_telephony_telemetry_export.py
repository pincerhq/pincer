"""
The exporter's one hard rule: telemetry must never stall the audio pipeline.

These tests hold the recorder to that — emit is synchronous and non-blocking, a
full queue drops and counts rather than waiting, a broken sink degrades to a
counter instead of an exception, and none of it reaches the caller.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pincer.voice.telemetry import context as ctxmod
from pincer.voice.telemetry.clock import mono_ns
from pincer.voice.telemetry.recorder import TelemetryRecorder
from pincer.voice.telemetry.records import TelemetryEvent, TelemetrySpan


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.spans: list[TelemetrySpan] = []

    async def write(self, events, spans) -> None:
        self.events.extend(events)
        self.spans.extend(spans)


class _BrokenSink:
    def __init__(self) -> None:
        self.attempts = 0

    async def write(self, events, spans) -> None:
        self.attempts += 1
        raise RuntimeError("sink is down")


class _SlowSink:
    """A sink that never returns — the worst case the queue must survive."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def write(self, events, spans) -> None:
        self.entered.set()
        await asyncio.sleep(3600)


@pytest.fixture(autouse=True)
def _clean_context():
    ctxmod.reset_for_tests()
    yield
    ctxmod.reset_for_tests()


def _ctx() -> ctxmod.CallContext:
    return ctxmod.register_call(provider_call_id="CA1", direction="inbound")


def _span(ctx: ctxmod.CallContext) -> TelemetrySpan:
    now = mono_ns()
    return TelemetrySpan(
        span_id=ctxmod.new_span_id(),
        call_id=ctx.call_id,
        name="llm.generation",
        start_utc="2026-01-01T00:00:00+00:00",
        start_mono_ns=now,
        end_mono_ns=now + 1_000_000,
        duration_ms=1.0,
    )


async def test_events_and_spans_reach_the_sink():
    sink = _CollectingSink()
    recorder = TelemetryRecorder(sink=sink, flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()
    recorder.event("turn.start", call=ctx)
    recorder.span(_span(ctx))
    await recorder.flush()
    assert len(sink.events) == 1
    assert len(sink.spans) == 1
    await recorder.stop()


async def test_a_full_queue_drops_and_counts_instead_of_blocking():
    sink = _SlowSink()
    recorder = TelemetryRecorder(sink=sink, queue_size=4, flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()

    started = time.monotonic()
    for _ in range(200):
        recorder.event("turn.start", call=ctx)
    elapsed = time.monotonic() - started

    # 200 emits onto a 4-slot queue: the point is that it did not wait.
    assert elapsed < 0.5
    stats = recorder.stats()
    assert stats.dropped_queue_full > 0
    assert stats.queued == 200
    assert stats.healthy is False
    await recorder.stop(drain_timeout_s=0.05)


async def test_emit_never_blocks_even_when_the_sink_hangs():
    """A wedged database must not turn into dead air on the call."""
    sink = _SlowSink()
    recorder = TelemetryRecorder(sink=sink, flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()
    recorder.event("turn.start", call=ctx)
    await asyncio.wait_for(sink.entered.wait(), timeout=1.0)

    started = time.monotonic()
    for _ in range(50):
        recorder.event("llm.first_token", call=ctx)
    assert time.monotonic() - started < 0.2
    await recorder.stop(drain_timeout_s=0.05)


async def test_export_failures_are_counted_and_surfaced_not_raised():
    sink = _BrokenSink()
    recorder = TelemetryRecorder(sink=sink, flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()
    recorder.event("turn.start", call=ctx)
    await recorder.flush()
    stats = recorder.stats()
    assert stats.export_failures > 0
    assert "sink is down" in stats.last_error
    assert stats.healthy is False
    await recorder.stop(drain_timeout_s=0.05)


async def test_a_failing_sink_is_backed_off_rather_than_hammered():
    sink = _BrokenSink()
    recorder = TelemetryRecorder(sink=sink, flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()
    for _ in range(20):
        recorder.event("turn.start", call=ctx)
        await recorder.flush()
    # One real attempt, then the backoff window absorbs the rest.
    assert sink.attempts <= 2
    await recorder.stop(drain_timeout_s=0.05)


async def test_coverage_is_one_on_an_idle_system():
    """No traffic is not a coverage problem; reporting 0% would page someone."""
    recorder = TelemetryRecorder(sink=_CollectingSink(), flush_interval_s=0.01)
    assert recorder.stats().coverage == 1.0


async def test_a_recorder_without_a_sink_drops_quietly():
    recorder = TelemetryRecorder(sink=None, enabled=False)
    ctx = _ctx()
    assert recorder.event("turn.start", call=ctx) is None
    assert recorder.stats().dropped_not_started == 1


async def test_emitting_outside_a_call_is_a_no_op():
    recorder = TelemetryRecorder(sink=_CollectingSink(), flush_interval_s=0.01)
    await recorder.start()
    assert recorder.event("turn.start") is None
    await recorder.stop(drain_timeout_s=0.05)


async def test_shutdown_is_bounded_even_with_a_wedged_sink():
    recorder = TelemetryRecorder(sink=_SlowSink(), flush_interval_s=0.01)
    await recorder.start()
    ctx = _ctx()
    recorder.event("turn.start", call=ctx)
    started = time.monotonic()
    await recorder.stop(drain_timeout_s=0.2)
    assert time.monotonic() - started < 2.0
