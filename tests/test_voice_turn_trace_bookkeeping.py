"""Which turn owns the open trace, and who is allowed to close it.

`_turn_traces` holds at most one open trace per call, and three places reach
for it: the barge-in canceller, the streaming task's cleanup and the buffered
path's. The streaming cleanup has always checked identity before touching the
dict; these tests hold the other two to the same rule, because the failure is
silent either way — a dropped trace is simply never persisted, and a
cross-closed one ends the wrong turn early.

Both LLM paths — streamed and buffered — run their turn as a tracked task in
`_active_turns`, so a barge-in cancels either one and takes its trace with it.
What is NOT in `_active_turns` is a turn still in its deterministic handlers
(gate, receptionist, goodbye): that one closes its own trace, and the canceller
must leave it alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import VoiceChannel


class FakeTrace:
    """Records what was done to it, so the assertions read as intent."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.cancelled_with: str | None = None
        self.finished = False

    def cancel(self, reason: str = "") -> None:
        self.cancelled_with = reason

    def stamp(self, *_args, **_kwargs) -> None:
        pass

    def finish(self) -> None:
        self.finished = True


def _channel() -> VoiceChannel:
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = apply_test_paths(MagicMock())
    settings.voice_language = "en-US"
    channel = VoiceChannel(settings)
    channel.set_engine(FakeVoiceEngine(settings))
    return channel


async def test_no_turn_in_flight_leaves_the_trace_to_its_owner():
    """No cancellable task means the open trace is not ours to take.

    A turn still in its deterministic handlers is not in `_active_turns`; the
    canceller used to pop its trace and return without finalising it — the
    turn's telemetry vanished before it could be written.
    """
    channel = _channel()
    trace = FakeTrace("in-flight")
    channel._turn_traces["CA1"] = trace

    await channel._cancel_prior_turn("CA1")

    assert channel._turn_traces.get("CA1") is trace, "the trace was taken from its owner"
    assert trace.cancelled_with is None
    assert trace.finished is False


async def test_barge_in_cancels_a_buffered_turn_and_its_trace():
    """End to end on the buffered path: the second utterance cancels the first
    handler call, its trace closes as a barge-in, and its answer is never spoken."""
    from pincer.voice.engine import CallDirection

    channel = _channel()
    engine = channel._engine
    engine.wait_for_turns = False
    traces: list[FakeTrace] = []

    def _open(call_sid, _state, _arrival_ns, _markers=None):
        traces.append(FakeTrace(f"turn-{len(traces) + 1}"))
        channel._turn_traces[call_sid] = traces[-1]
        return traces[-1]

    channel._open_turn_trace = _open
    entered = asyncio.Event()

    async def _handler(incoming):
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()  # the first answer is never produced
        return "Second answer."

    await channel.start(_handler)
    await engine.on_call_start("CA1", "+4930123456", CallDirection.INBOUND)

    await engine.on_speech_input("CA1", "First question?")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await engine.on_speech_input("CA1", "Actually, something else.")
    await channel.wait_for_turn("CA1", timeout=2)

    assert traces[0].cancelled_with == "barge_in"
    assert traces[1].cancelled_with is None and traces[1].finished
    assert engine.spoken["CA1"] == ["Second answer."]
    await channel.stop()


async def test_barge_in_cancels_a_streaming_turn_and_its_trace():
    """The case that does own the trace still behaves exactly as before."""
    channel = _channel()
    trace = FakeTrace("streaming")
    channel._turn_traces["CA1"] = trace

    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(never_finishes())
    await started.wait()
    channel._active_turns["CA1"] = task

    await channel._cancel_prior_turn("CA1")

    assert trace.cancelled_with == "barge_in"
    assert task.cancelled()
    assert "CA1" not in channel._turn_traces


async def test_a_superseded_turn_closes_its_own_trace_not_the_successors():
    """Identity, not position: closing must not reach for whatever is current."""
    channel = _channel()
    mine = FakeTrace("mine")
    successor = FakeTrace("successor")
    channel._turn_traces["CA1"] = successor

    channel._close_turn_trace("CA1", own=mine)

    assert mine.finished is True
    assert successor.finished is False
    assert channel._turn_traces.get("CA1") is successor, "the successor was closed early"


async def test_a_deterministic_handler_closes_its_own_trace_not_the_successors():
    """The early returns (mutual goodbye, approval gate, receptionist) pass `own=`."""
    channel = _channel()
    mine = FakeTrace("mine")
    successor = FakeTrace("successor")
    channel._turn_traces["CA1"] = successor

    channel._close_turn_trace("CA1", "approval_gate", own=mine)

    assert mine.finished is True
    assert successor.finished is False, "the successor was closed by the wrong turn"
    assert channel._turn_traces.get("CA1") is successor


async def test_the_gates_early_return_leaves_a_later_turns_trace_open():
    """The real path: a later turn registers while the gate is still deciding."""
    from pincer.voice.engine import CallDirection
    from pincer.voice.in_call_tools import CallerVerdict

    channel = _channel()

    async def _handler(_incoming):
        return ""

    await channel.start(_handler)
    await channel._engine.on_call_start("CA1", "+4930123456", CallDirection.INBOUND)
    mine, successor = FakeTrace("mine"), FakeTrace("successor")

    def _open(call_sid, _state, _arrival_ns, _markers=None):
        channel._turn_traces[call_sid] = mine
        return mine

    channel._open_turn_trace = _open

    class SlowGate:
        def begin_turn(self) -> None: ...

        async def handle_caller_utterance(self, _text):
            channel._turn_traces["CA1"] = successor  # the next utterance got in first
            return CallerVerdict(status="unclear", handled=True)

    channel._tool_gates["CA1"] = SlowGate()

    await channel._handle_speech_turn("CA1", "Moment")

    assert mine.finished is True
    assert successor.finished is False, "the gate's turn closed its successor's trace"
    assert channel._turn_traces.get("CA1") is successor
    await channel.stop()


async def test_closing_without_an_owner_still_takes_whatever_is_open():
    """Teardown has no turn of its own and must keep the old behaviour."""
    channel = _channel()
    trace = FakeTrace("open")
    channel._turn_traces["CA1"] = trace

    channel._close_turn_trace("CA1")

    assert trace.finished is True
    assert "CA1" not in channel._turn_traces
