"""A listed OpenAI-wire endpoint streams on the Responses API (issue #358, slice 7).

`stream_turn` must give the caller what the Chat Completions path gives: text deltas in order, then one
final response. The OpenAI SDK's own client runs on a MockTransport (tests/openai_wire.py) and parses
scripted server-sent events.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from openai_wire import (
    FIXTURES,
    GOLDEN_SCENARIOS,
    PARTIAL_ENCRYPTED,
    Call,
    RawStream,
    ScriptedOpenAI,
    Turn,
    function_call_item,
    golden_chat_request,
    message_item,
    reasoning_item,
    response_body,
    responses_events,
)
from pydantic import SecretStr

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, StreamTurnEvent

_HI = [LLMMessage(role=MessageRole.USER, content="hi")]


@pytest.fixture
def openai_settings(settings):
    """OpenAI on a reasoning model, the developer's effort/thinking env pinned empty."""
    return settings.model_copy(
        update={
            "default_provider": "openai",
            "openai_model": "gpt-5.2",
            "openai_api_key": SecretStr("sk-test"),
            "reasoning_effort": "",
            "thinking": "",
            "max_tokens": 4096,
            "temperature": 0.5,
        }
    )


async def test_a_listed_endpoint_streams_on_the_responses_api(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [Turn(text=["Hel", "lo"], reasoning=True)])
    events = [e async for e in llm.provider.stream_turn(_HI)]

    assert llm.paths == ["/v1/responses"]  # precondition: the Responses API, not Chat Completions
    assert llm.requests[0] == {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
        "stream": True,
    }
    assert [e.text for e in events] == ["Hel", "lo", ""]
    final = events[-1].response
    assert final is not None
    assert [e.response for e in events[:-1]] == [None, None]
    assert (final.content, final.stop_reason, final.wire) == ("Hello", "stop", "openai:gpt-5.2")
    assert [item["type"] for item in final.raw] == ["reasoning", "message"]


# ── Parity with Chat Completions ─────────────────────────────────────────────

_TOOL_ARGS = ['{"ci', 'ty": "Kyiv"}']
_TURNS = {
    "text": Turn(text=["Hel", "lo ", "there."]),
    "text and two tool calls": Turn(
        text=["Let me ", "check."],
        calls=[Call("get_weather", _TOOL_ARGS, "call_1"), Call("note", ["{", "}"], "call_2")],
    ),
    "a tool call and no text": Turn(calls=[Call("note", ["{", "}"])]),
    "cached input": Turn(text=["ok"], prompt_tokens=1000, cached=600, completion_tokens=200),
    "cut off by the output limit": Turn(text=["cut off"], truncated=True),
}


def _seen(events: list[StreamTurnEvent]) -> list[tuple[str, dict[str, object] | None]]:
    """Each event as (its text, the final response's fields) - everything but `raw` and `wire`."""

    def fields(r: LLMResponse | None) -> dict[str, object] | None:
        if r is None:
            return None
        return {
            "content": r.content,
            "tool_calls": [(c.id, c.name, c.arguments) for c in r.tool_calls],
            "stop_reason": r.stop_reason,
            "model": r.model,
            "provider": r.provider,
            "request_model": r.request_model,
            "usage": (r.input_tokens, r.cache_read_tokens, r.cache_write_5m_tokens, r.output_tokens),
        }

    return [(e.text, fields(e.response)) for e in events]


@pytest.mark.parametrize("turn", _TURNS.values(), ids=_TURNS.keys())
async def test_the_same_scripted_stream_gives_the_same_events_on_both_apis(openai_settings, turn):
    on_chat = openai_settings.model_copy(update={"responses_api_providers": []})
    chat = ScriptedOpenAI(on_chat, [turn])
    responses = ScriptedOpenAI(openai_settings, [turn])
    chat_events = [e async for e in chat.provider.stream_turn(_HI)]
    responses_events = [e async for e in responses.provider.stream_turn(_HI)]

    assert (chat.paths, responses.paths) == (["/v1/chat/completions"], ["/v1/responses"])  # precondition
    assert len(chat_events) == len(turn.text) + 1  # precondition: the script was streamed in pieces
    assert _seen(responses_events) == _seen(chat_events)
    chat_final, responses_final = chat_events[-1].response, responses_events[-1].response
    assert chat_final is not None and responses_final is not None
    assert chat_final.raw == []
    assert [item["type"] for item in responses_final.raw] == [i["type"] for i in turn.items()]


# ── The final message holds the completed items ──────────────────────────────

_TOOL_TURN_ITEMS = [
    reasoning_item("rs_1", "ENC-1"),
    message_item("Checking."),
    function_call_item("get_weather", {"city": "Kyiv"}, "call_1", "fc_1"),
]


def _terminal_with_partial_reasoning(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """The events with the terminal response carrying the reasoning item as `added` sent it."""
    *before, last = events
    response = dict(last["response"])  # type: ignore[call-overload]
    response["output"] = [
        {**i, "encrypted_content": PARTIAL_ENCRYPTED} if i["type"] == "reasoning" else i for i in _TOOL_TURN_ITEMS
    ]
    return [*before, {**last, "response": response}]


@pytest.mark.parametrize("terminal_output", ["empty", "partial reasoning"])
async def test_the_final_message_holds_the_items_of_the_done_events_not_the_in_progress_ones(
    openai_settings, terminal_output
):
    events = responses_events(_TOOL_TURN_ITEMS, terminal_output=False)
    assert events[-1]["response"]["output"] == []  # type: ignore[index]  # precondition: the items are only in `done`
    if terminal_output == "partial reasoning":
        events = _terminal_with_partial_reasoning(responses_events(_TOOL_TURN_ITEMS))
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert added[0]["item"]["encrypted_content"] == PARTIAL_ENCRYPTED  # type: ignore[index]  # precondition
    llm = ScriptedOpenAI(openai_settings, [RawStream(events)])
    final = [e async for e in llm.provider.stream_turn(_HI)][-1].response

    assert final is not None
    assert final.raw == _TOOL_TURN_ITEMS  # in output order, the reasoning item complete
    assert final.content == "Checking."
    assert [(c.id, c.name, c.arguments) for c in final.tool_calls] == [("call_1", "get_weather", {"city": "Kyiv"})]
    assert final.stop_reason == "tool_calls"


# ── Failures inside a stream ─────────────────────────────────────────────────


async def _drain(provider) -> tuple[list[StreamTurnEvent], LLMError | None]:
    """The events a stream gave before it raised (or all of them) and the LLMError, if any."""
    seen: list[StreamTurnEvent] = []
    try:
        async for event in provider.stream_turn(_HI):
            seen.append(event)
    except LLMError as error:
        return seen, error
    return seen, None


def _up_to_the_text() -> list[dict[str, object]]:
    """A reply's events up to and including its first text delta."""
    events = responses_events([message_item("Hello")])
    return events[: next(i for i, e in enumerate(events) if e["type"] == "response.output_text.delta") + 1]


def _failed(code: str, message: str = "boom") -> dict[str, object]:
    body = {**response_body([], status="failed"), "error": {"code": code, "message": message}}
    return {"type": "response.failed", "sequence_number": 90, "response": body}


def _error_event(code: str, message: str = "boom") -> dict[str, object]:
    return {"type": "error", "sequence_number": 90, "code": code, "message": message, "param": None}


def _azure_error(kind: str, message: str = "boom") -> dict[str, object]:
    """Azure's mid-stream error: an HTTP 200 whose event nests the error (the SDK raises it itself)."""
    return {"type": "error", "error": {"type": kind, "code": "x", "message": message, "param": None}}


_FAILURES = {
    "response.failed": (_failed("server_error"), True),
    "response.failed, not worth asking again": (_failed("invalid_prompt"), False),
    "error event": (_error_event("server_error"), True),
    "Azure error payload": (_azure_error("server_error"), True),
}


@pytest.mark.parametrize("failure,retryable", _FAILURES.values(), ids=_FAILURES.keys())
async def test_a_stream_that_fails_raises_an_llm_error_after_the_text_it_already_gave(
    openai_settings, failure, retryable
):
    llm = ScriptedOpenAI(openai_settings, [RawStream([*_up_to_the_text(), failure])])
    events, error = await _drain(llm.provider)

    assert [e.text for e in events] == ["Hello"]  # precondition: the text reached the caller first
    assert error is not None
    assert "openai stream error" in str(error) and "boom" in str(error)
    assert error.retryable is retryable


async def test_a_stream_that_ends_without_a_final_event_raises_a_retryable_error(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [RawStream(_up_to_the_text())])
    events, error = await _drain(llm.provider)

    assert [e.text for e in events] == ["Hello"]  # precondition
    assert error is not None and error.retryable
    assert "ended without a final response" in str(error)


# ── 429: retried before the first event, never after ─────────────────────────

_SLEEP = "pincer.llm.openai_common.asyncio.sleep"
_RATE_LIMIT_SIGNALS = {
    "response.failed": _failed("rate_limit_exceeded"),
    "Azure too_many_requests": _azure_error("too_many_requests"),
}


async def test_a_429_before_the_first_event_is_retried_after_a_wait(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [429, Turn(text=["Back."])])
    with patch(_SLEEP, new=AsyncMock()) as sleep:
        events, error = await _drain(llm.provider)

    assert llm.paths == ["/v1/responses", "/v1/responses"]
    assert error is None
    assert events[0].text == "Back."
    sleep.assert_awaited_once()


async def test_a_429_that_lasts_raises_after_the_attempts(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [429, 429, 429])
    with patch(_SLEEP, new=AsyncMock()), pytest.raises(LLMRateLimitError):
        [e async for e in llm.provider.stream_turn(_HI)]

    assert llm.paths == ["/v1/responses"] * 3


@pytest.mark.parametrize("signal", _RATE_LIMIT_SIGNALS.values(), ids=_RATE_LIMIT_SIGNALS.keys())
async def test_a_rate_limit_inside_the_stream_before_any_text_is_retried(openai_settings, signal):
    """Inferred, not documented for OpenAI: a stream that fails with a rate-limit code is treated as a 429."""
    first = RawStream([responses_events([message_item("x")])[0], signal])
    llm = ScriptedOpenAI(openai_settings, [first, Turn(text=["Back."])])
    with patch(_SLEEP, new=AsyncMock()) as sleep:
        events, error = await _drain(llm.provider)

    assert llm.paths == ["/v1/responses", "/v1/responses"]
    assert error is None
    assert events[0].text == "Back."
    sleep.assert_awaited_once()


@pytest.mark.parametrize("signal", _RATE_LIMIT_SIGNALS.values(), ids=_RATE_LIMIT_SIGNALS.keys())
async def test_a_rate_limit_after_the_first_text_is_not_retried(openai_settings, signal):
    llm = ScriptedOpenAI(openai_settings, [RawStream([*_up_to_the_text(), signal]), Turn(text=["Again."])])
    seen: list[StreamTurnEvent] = []
    with patch(_SLEEP, new=AsyncMock()) as sleep, pytest.raises(LLMRateLimitError):
        async for event in llm.provider.stream_turn(_HI):
            seen.append(event)

    assert [e.text for e in seen] == ["Hello"]  # precondition: the caller already had text
    assert llm.paths == ["/v1/responses"]  # one request: nothing resent
    sleep.assert_not_awaited()


# ── Events that are none of the caller's business ────────────────────────────


async def test_events_about_reasoning_compaction_or_unknown_to_the_sdk_are_ignored(openai_settings):
    base = responses_events([reasoning_item(), message_item("Hello")])
    noise = [
        {"type": "response.in_progress", "sequence_number": 50, "response": base[0]["response"]},
        {
            "type": "response.reasoning_summary_text.delta",
            "sequence_number": 51,
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "delta": "SUMMARY-DELTA",
        },
        {
            "type": "response.reasoning_text.delta",
            "sequence_number": 52,
            "item_id": "rs_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "REASONING-DELTA",
        },
        {"type": "response.compaction.compacting", "sequence_number": 53, "item_id": "cmp_1", "output_index": 2},
        {"type": "response.invented_next_year", "sequence_number": 54, "delta": "INVENTED-DELTA"},
    ]
    llm = ScriptedOpenAI(openai_settings, [RawStream([base[0], *noise, *base[1:]])])
    events = [e async for e in llm.provider.stream_turn(_HI)]

    assert llm.paths == ["/v1/responses"]  # precondition: the stream was read
    assert [e.text for e in events] == ["Hello", ""]
    assert events[-1].response is not None and events[-1].response.content == "Hello"


# ── A stream the caller walks away from ──────────────────────────────────────


async def test_a_caller_that_stops_reading_closes_the_http_stream_and_gets_no_final_response(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [RawStream(_up_to_the_text(), hang=True)])
    stream = llm.provider.stream_turn(_HI)
    first = await asyncio.wait_for(anext(stream), timeout=5)

    assert (first.text, first.response) == ("Hello", None)  # precondition: mid-reply
    assert not llm.streams[0].closed  # precondition: the connection is still open
    await stream.aclose()
    assert llm.streams[0].closed


async def test_a_cancelled_read_closes_the_http_stream(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [RawStream(_up_to_the_text(), hang=True)])
    got_text = asyncio.Event()
    final: list[StreamTurnEvent] = []

    async def read() -> None:
        async for event in llm.provider.stream_turn(_HI):
            if event.response is not None:
                final.append(event)
            else:
                got_text.set()

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(got_text.wait(), timeout=5)
        assert not llm.streams[0].closed  # precondition: the reply is still being generated
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert llm.streams[0].closed
    assert final == []


# ── Chat Completions endpoints are untouched ─────────────────────────────────


@pytest.mark.parametrize("scenario", list(GOLDEN_SCENARIOS))
async def test_an_unlisted_endpoint_streams_the_chat_completions_body_it_always_did(settings, scenario):
    """The golden bodies were captured from commit 15aa37f, before `stream_turn` learned the Responses API:
    system, summary, context + photo + words, a tool call and its result, `stream_options`, the cache key."""
    path, body = await golden_chat_request(settings, scenario, stream=True)

    assert path == "/v1/chat/completions"
    assert body == (FIXTURES / f"openai_chat_stream_body_15aa37f_{scenario}.json").read_text()
