"""Which turn owns the open trace, and who is allowed to close it.

`_turn_traces` holds at most one open trace per call, and three places reach
for it: the barge-in canceller, the streaming task's cleanup and the buffered
path's. The streaming cleanup has always checked identity before touching the
dict; these tests hold the other two to the same rule, because the failure is
silent either way — a dropped trace is simply never persisted, and a
cross-closed one ends the wrong turn early.

Only the streaming pipeline populates `_active_turns`, and the live
VoiceChannel always calls `set_stream_agent`, so the buffered path is not
reachable in production today. That is precisely why it needs a test: nothing
else would notice it rotting.
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


async def test_barge_in_leaves_a_buffered_turns_trace_to_its_owner():
    """No cancellable task means the open trace is not ours to take.

    The buffered path never fills `_active_turns`, so the canceller used to pop
    the trace and then return without finalising it — the turn's telemetry
    vanished before it could be written.
    """
    channel = _channel()
    trace = FakeTrace("in-flight")
    channel._turn_traces["CA1"] = trace

    await channel._cancel_prior_turn("CA1")

    assert channel._turn_traces.get("CA1") is trace, "the trace was taken from its owner"
    assert trace.cancelled_with is None
    assert trace.finished is False


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


async def test_closing_without_an_owner_still_takes_whatever_is_open():
    """Teardown has no turn of its own and must keep the old behaviour."""
    channel = _channel()
    trace = FakeTrace("open")
    channel._turn_traces["CA1"] = trace

    channel._close_turn_trace("CA1")

    assert trace.finished is True
    assert "CA1" not in channel._turn_traces
