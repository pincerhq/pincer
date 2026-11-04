"""Cut-off voice turns on a call: retry line, own label and log, own counter (issue #345, Q8c/Q8d)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from test_voice_streaming_turn import _settings, text_chunks

from pincer.channels.phone_calls import VoiceChannel
from pincer.exceptions import LLMOutputTruncatedError
from pincer.voice import scheduling
from pincer.voice.engine import CallDirection
from pincer.voice.prompts import get_prompt

if TYPE_CHECKING:
    from pincer.core.agent import StreamChunk

CALL = "CA_cut"
RETRY_LINE = str(get_prompt("BRAIN_ERROR_RETRY", "de", "sie"))
FINAL_LINE = str(get_prompt("BRAIN_ERROR_FINAL", "de", "sie"))


def _truncated() -> LLMOutputTruncatedError:
    return LLMOutputTruncatedError(4096, 880)


class _Trace:
    """Turn trace stand-in: records how the turn failed, ignores the rest."""

    def __init__(self) -> None:
        self.failed_with: str | None = None

    def fail(self, error: str) -> None:
        self.failed_with = error

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None


class _Agent:
    """One scripted outcome per turn: the chunks to stream, or an exception to raise."""

    def __init__(self, turns: list[list[StreamChunk] | BaseException]) -> None:
        self.turns = list(turns)

    async def stream_voice_turn(self, **_kwargs):
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        for chunk in turn:
            yield chunk


async def _call(turns=None, *, handler=None):
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    if turns is not None:
        channel.set_stream_agent(_Agent(turns))
    traces: list[_Trace] = []

    def _open_trace(call_sid, _state, _arrival_ns, _markers=None):
        traces.append(_Trace())
        channel._turn_traces[call_sid] = traces[-1]
        return traces[-1]

    channel._open_turn_trace = _open_trace  # type: ignore[method-assign]
    await channel.start(handler or AsyncMock(return_value=""))
    await engine.on_call_start(CALL, "+4930123456", CallDirection.INBOUND, language="de")
    return channel, engine, traces


@pytest.fixture(autouse=True)
def _clean_scheduling():
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


async def test_streaming_turn_cut_off_asks_again_and_keeps_the_call(caplog):
    channel, engine, traces = await _call([_truncated()])

    with caplog.at_level(logging.WARNING, logger="pincer.channels.phone_calls"):
        await engine.on_speech_input(CALL, "Ich brauche einen Termin.")

    assert engine.spoken[CALL][-1] == RETRY_LINE
    assert CALL not in engine.ended
    assert traces[-1].failed_with == "output_truncated"
    records = [r for r in caplog.records if r.name == "pincer.channels.phone_calls" and r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    message = records[0].getMessage()
    assert CALL in message and "4096" in message and "880" in message
    await channel.stop()


async def test_buffered_turn_cut_off_asks_again_and_keeps_the_call(caplog):
    channel, engine, traces = await _call(handler=AsyncMock(side_effect=_truncated()))

    with caplog.at_level(logging.WARNING, logger="pincer.channels.phone_calls"):
        await engine.on_speech_input(CALL, "Ich brauche einen Termin.")

    assert engine.spoken[CALL][-1] == RETRY_LINE
    assert CALL not in engine.ended
    assert traces[-1].failed_with == "output_truncated"
    records = [r for r in caplog.records if r.name == "pincer.channels.phone_calls" and r.levelno >= logging.WARNING]
    assert [(r.levelno, r.exc_info) for r in records] == [(logging.WARNING, None)]
    await channel.stop()


def _ok() -> list[StreamChunk]:
    return text_chunks("Gerne, das mache ich.")


async def _say(engine, times: int) -> None:
    for _ in range(times):
        await engine.on_speech_input(CALL, "Hallo?")


async def test_third_truncated_turn_in_a_row_ends_the_call():
    channel, engine, _traces = await _call([_truncated(), _truncated(), _truncated()])

    await _say(engine, 2)
    assert CALL not in engine.ended
    assert engine.spoken[CALL].count(RETRY_LINE) == 2

    sm = channel._state_machines[CALL]
    await _say(engine, 1)
    assert engine.spoken[CALL][-1] == FINAL_LINE
    assert CALL in engine.ended
    assert sm.state.transitions[-1].reason == "repeated_llm_truncations"
    await channel.stop()


async def test_a_good_turn_resets_the_truncation_count():
    channel, engine, _traces = await _call([_truncated(), _truncated(), _ok(), _truncated(), _truncated()])

    await _say(engine, 5)

    assert CALL not in engine.ended
    assert FINAL_LINE not in engine.spoken[CALL]
    await channel.stop()


async def test_a_truncation_does_not_reset_the_error_count():
    channel, engine, _traces = await _call([RuntimeError("LLM down"), _truncated(), RuntimeError("LLM down")])

    await _say(engine, 3)

    assert engine.spoken[CALL][-1] == FINAL_LINE
    assert CALL in engine.ended
    await channel.stop()


async def test_truncations_and_errors_keep_separate_counts():
    channel, engine, _traces = await _call([_truncated(), _truncated(), RuntimeError("LLM down"), _truncated()])

    await _say(engine, 3)
    assert CALL not in engine.ended  # 2 truncations + 1 error: neither limit reached

    await _say(engine, 1)  # the error did not reset the truncation count
    assert engine.spoken[CALL][-1] == FINAL_LINE
    assert CALL in engine.ended
    await channel.stop()


def test_repeated_truncations_classify_as_an_llm_failure():
    from pincer.observability.failure_codes import FailureCode, classify_failure

    assert classify_failure("repeated_llm_truncations") is FailureCode.LLM_ERROR


async def test_buffered_truncations_count_apart_from_errors():
    handler = AsyncMock(side_effect=[_truncated(), _truncated(), RuntimeError("LLM down"), _truncated()])
    channel, engine, _traces = await _call(handler=handler)

    await _say(engine, 3)
    assert CALL not in engine.ended  # 2 truncations + 1 error: neither limit reached

    await _say(engine, 1)
    assert engine.spoken[CALL][-1] == FINAL_LINE
    assert CALL in engine.ended
    await channel.stop()


async def test_call_end_forgets_the_truncation_count():
    channel, engine, _traces = await _call([_truncated()])
    await _say(engine, 1)
    assert channel._truncation_counts.get(CALL) == 1  # precondition: the count exists

    await engine.end_call(CALL)

    assert CALL not in channel._truncation_counts
    await channel.stop()
