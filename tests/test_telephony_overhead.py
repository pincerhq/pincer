"""
Instrumentation overhead, measured rather than asserted by hope.

Two questions this file answers, and prints, so the numbers land in CI output
and in the engineering guide:

1. What does one instrumented turn cost on the audio path?
2. Under representative call concurrency, can telemetry export stall audio?

The thresholds are deliberately loose — this runs on developer laptops and CI
boxes of unknown speed. They are there to catch an ORDER-OF-MAGNITUDE
regression (a blocking write sneaking onto the hot path), not to police
microseconds.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pincer.db import ensure_schema_current
from pincer.voice.telemetry import runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.schema import EventName, SpanName
from pincer.voice.telemetry.tracer import drain_pending

MS = 1_000_000

#: A turn emits ~8 events and ~3 spans. Anything approaching a millisecond of
#: CPU per turn would be a blocking call that has no business being here.
MAX_US_PER_TURN = 2000.0

#: Representative pilot concurrency. Each call emits a turn's worth of records.
CONCURRENT_CALLS = 25

#: An "audio write" the export must not delay. Real frames are every 20 ms; a
#: scheduling delay beyond this means telemetry got in front of the audio.
MAX_AUDIO_STALL_MS = 50.0

#: The single worst sample is judged separately and more loosely. On a shared
#: CI runner one 20 ms tick can land on an OS preemption that has nothing to do
#: with telemetry; a blocking write on the loop shows up as REPEATED stalls, so
#: the 95th percentile carries the acceptance criterion and this ceiling only
#: catches a single gross block.
MAX_SINGLE_STALL_MS = 200.0


def _instrument_one_turn(tracer) -> None:
    """Exactly what a live streaming turn emits, in the same order."""
    origin = tracer.watch.started_ns
    turn = tracer.start_turn(speech_end_ns=origin)
    turn.stamp(EventName.STT_FINAL, at_ns=origin + 10 * MS)
    turn.stamp(EventName.AGENT_PREP_DONE, at_ns=origin + 20 * MS)
    turn.stamp(EventName.LLM_REQUEST, at_ns=origin + 20 * MS)
    llm = turn.open_span(SpanName.LLM)
    turn.stamp(EventName.LLM_FIRST_TOKEN, at_ns=origin + 200 * MS)
    llm.close()
    tts = turn.open_span(SpanName.TTS)
    turn.stamp(EventName.TTS_FIRST_AUDIO, at_ns=origin + 300 * MS)
    tts.close()
    turn.first_audio(kind="audio", at_ns=origin + 320 * MS)
    turn.stamp(EventName.LLM_DONE, at_ns=origin + 500 * MS)
    turn.finish()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "telephony.db"
    ensure_schema_current(path)
    yield path
    asyncio.run(runtime.shutdown())
    runtime.reset_for_tests()


async def test_instrumentation_overhead_per_turn_is_measured(db, capsys):
    runtime.reset_for_tests(db_path=str(db))
    await get_recorder().start()
    tracer = runtime.start_call(provider_call_id="CA_overhead", direction="inbound", engine="media_streams")
    assert tracer is not None

    turns = 300
    started = time.perf_counter()
    for _ in range(turns):
        _instrument_one_turn(tracer)
    elapsed = time.perf_counter() - started
    per_turn_us = (elapsed / turns) * 1_000_000

    # Disabled telemetry is the control: the difference is what instrumentation costs.
    runtime.reset_for_tests(db_path="", enabled_=False)
    disabled_tracer_absent = runtime.start_call(provider_call_id="CA_off", direction="inbound")
    assert disabled_tracer_absent is None

    with capsys.disabled():
        print(f"\n[overhead] {per_turn_us:.0f} µs of CPU per instrumented turn (~11 records)")

    assert per_turn_us < MAX_US_PER_TURN, f"instrumentation cost {per_turn_us:.0f}µs/turn"


async def test_telemetry_export_cannot_stall_audio_under_concurrency(db, capsys):
    """The acceptance criterion, exercised directly.

    While `CONCURRENT_CALLS` calls emit telemetry, a simulated audio writer runs
    on the same loop every 20 ms. Its scheduling delay is what a caller would
    hear as a gap, so that is what is measured.
    """
    runtime.reset_for_tests(db_path=str(db))
    await get_recorder().start()

    stalls: list[float] = []
    stop = asyncio.Event()

    async def audio_writer() -> None:
        previous = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            stalls.append((now - previous) * 1000.0 - 20.0)
            previous = now

    writer = asyncio.create_task(audio_writer())

    async def one_call(index: int) -> None:
        tracer = runtime.start_call(provider_call_id=f"CA_load_{index}", direction="inbound", engine="media_streams")
        assert tracer is not None
        tracer.answered()
        for _ in range(8):
            _instrument_one_turn(tracer)
            await asyncio.sleep(0)
        await tracer.finish(status="completed", failure_code="none", duration_s=30.0)

    started = time.perf_counter()
    await asyncio.gather(*(one_call(i) for i in range(CONCURRENT_CALLS)))
    wall = time.perf_counter() - started
    stop.set()
    await writer

    await drain_pending()
    await get_recorder().flush()

    ordered = sorted(stalls) or [0.0]
    worst = ordered[-1]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    stats = get_recorder().stats()
    with capsys.disabled():
        print(
            f"[overhead] {CONCURRENT_CALLS} concurrent calls × 8 turns in {wall * 1000:.0f} ms; "
            f"audio-loop delay p95 {p95:.1f} ms, worst {worst:.1f} ms over {len(stalls)} ticks; "
            f"{stats.exported} records exported, {stats.dropped_queue_full} dropped"
        )

    assert p95 < MAX_AUDIO_STALL_MS, f"audio loop p95 delay was {p95:.1f}ms"
    assert worst < MAX_SINGLE_STALL_MS, f"audio loop was blocked for {worst:.1f}ms in one tick"
    assert stats.export_failures == 0


async def test_a_wedged_sink_costs_records_not_latency(db, capsys):
    """When export cannot keep up, the queue drops — it never applies backpressure."""
    from pincer.voice.telemetry.recorder import TelemetryRecorder, set_recorder

    class _Wedged:
        async def write(self, events, spans) -> None:
            await asyncio.sleep(3600)

    runtime.reset_for_tests(db_path=str(db))
    recorder = TelemetryRecorder(sink=_Wedged(), queue_size=32, flush_interval_s=0.01)
    set_recorder(recorder)
    await recorder.start()

    tracer = runtime.start_call(provider_call_id="CA_wedged", direction="inbound", engine="media_streams")
    assert tracer is not None

    started = time.perf_counter()
    for _ in range(100):
        _instrument_one_turn(tracer)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    stats = recorder.stats()
    with capsys.disabled():
        print(
            f"[overhead] wedged sink: 100 turns emitted in {elapsed_ms:.0f} ms, "
            f"{stats.dropped_queue_full} records dropped (queue cap {stats.queue_capacity})"
        )

    assert stats.dropped_queue_full > 0
    assert elapsed_ms < 1000.0, "a wedged sink applied backpressure to the audio path"
    await recorder.stop(drain_timeout_s=0.05)
