"""Every conversation path on a Responses endpoint keeps the model's reasoning (issue #358, slice 7).

Web chat and voice stream on `/responses`; `run_headless` and the one-shot internal calls use it too.
The OpenAI SDK's own client runs on a MockTransport (tests/openai_wire.py).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from openai_wire import (
    Incomplete,
    RawStream,
    ScriptedOpenAI,
    function_call_item,
    message_item,
    reasoning_item,
    responses_events,
)
from pydantic import SecretStr

from pincer.core.agent import Agent, StreamEventType, _Onboarding
from pincer.core.compaction import Compactor
from pincer.exceptions import LLMOutputTruncatedError
from pincer.llm.base import LLMMessage, MessageRole
from pincer.mcp.sampling import StandaloneLLMBackend
from pincer.tools.registry import ToolRegistry
from pincer.voice.outcome import extract_outcome
from pincer.voice.threads import merge_summary

_FIRST = [reasoning_item("rs_1", "ENC-1"), function_call_item("lookup", {"q": "tea"}, "call_1", "fc_1")]
_SECOND = [reasoning_item("rs_2", "ENC-2"), message_item("Green tea.", "msg_2")]
_THIRD = [reasoning_item("rs_3", "ENC-3"), message_item("Anything else?", "msg_3")]
_USER, _CALLER = "user1", "caller1"


@pytest.fixture
def openai_settings(settings):
    """OpenAI on a reasoning model, the developer's effort/thinking/voice env pinned."""
    return settings.model_copy(
        update={
            "default_provider": "openai",
            "openai_model": "gpt-5.2",
            "openai_api_key": SecretStr("sk-test"),
            "reasoning_effort": "",
            "thinking": "",
            "max_tokens": 4096,
            "temperature": 0.5,
            "voice_turn_model": "",
            "voice_max_response_tokens": 220,
        }
    )


def _agent(settings, session_manager, cost_tracker, replies):
    registry = ToolRegistry()

    async def lookup(q: str = "") -> str:
        return f"found {q}"

    registry.register(name="lookup", description="Look something up", handler=lookup)
    llm = ScriptedOpenAI(settings, replies)
    return Agent(settings, llm.provider, session_manager, cost_tracker, registry), llm


def _kinds(request: dict[str, Any]) -> list[str]:
    """What each `input` item is: its type, or its role for a message."""
    return [item.get("type") or item["role"] for item in request["input"]]


_TWO_TURNS_KINDS = ["user", "reasoning", "function_call", "function_call_output", "reasoning", "message", "user"]


# ── Web chat ─────────────────────────────────────────────────────────────────


async def test_streamed_web_chat_sends_the_reasoning_within_a_turn_across_turns_and_after_a_reload(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete(_USER, "web", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND, _THIRD, _SECOND])
    [c async for c in agent.handle_message_stream(_USER, "web", "Which tea?", memory=False)]

    assert llm.paths == ["/v1/responses"] * 2  # precondition: a tool turn streamed two requests
    assert all(request["stream"] is True for request in llm.requests)
    assert _kinds(llm.requests[1]) == ["user", "reasoning", "function_call", "function_call_output"]
    assert llm.requests[1]["input"][1:3] == _FIRST  # within the turn: the API's own items, in its order

    [c async for c in agent.handle_message_stream(_USER, "web", "And coffee?", memory=False)]
    assert _kinds(llm.requests[2]) == _TWO_TURNS_KINDS
    assert llm.requests[2]["input"][1:3] == _FIRST and llm.requests[2]["input"][4:6] == _SECOND

    stored = (await session_manager.get_or_create(_USER, "web")).messages
    saved = [m.to_dict() for m in stored if m.role == MessageRole.ASSISTANT]
    assert [(m["wire"], [i["type"] for i in m["raw"]]) for m in saved] == [
        ("openai:gpt-5.2", ["reasoning", "function_call"]),
        ("openai:gpt-5.2", ["reasoning", "message"]),
        ("openai:gpt-5.2", ["reasoning", "message"]),
    ]

    before = await session_manager.get_or_create(_USER, "web")
    await session_manager.evict("web:user1")
    [c async for c in agent.handle_message_stream(_USER, "web", "Thanks", memory=False)]
    assert await session_manager.get_or_create(_USER, "web") is not before  # precondition: loaded from storage
    turn_3 = llm.requests[3]["input"]
    assert [i["id"] for i in turn_3 if i.get("type") == "reasoning"] == ["rs_1", "rs_2", "rs_3"]


# ── Voice ────────────────────────────────────────────────────────────────────


async def test_voice_turns_send_the_reasoning_and_it_survives_the_end_of_the_call(
    openai_settings, session_manager, cost_tracker
):
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND, _THIRD, _SECOND])
    first = [c async for c in agent.stream_voice_turn(_CALLER, "voice", "Which tea?")]

    assert llm.paths == ["/v1/responses"] * 2  # precondition: a tool turn streamed two requests
    assert [r["max_output_tokens"] for r in llm.requests] == [220, 220]  # precondition: the voice request
    assert first[-1].content == "Green tea."
    assert _kinds(llm.requests[1]) == ["user", "reasoning", "function_call", "function_call_output"]
    assert llm.requests[1]["input"][1:3] == _FIRST

    [c async for c in agent.stream_voice_turn(_CALLER, "voice", "And coffee?")]
    assert _kinds(llm.requests[2]) == _TWO_TURNS_KINDS
    assert llm.requests[2]["input"][1:3] == _FIRST and llm.requests[2]["input"][4:6] == _SECOND

    session = await session_manager.get_or_create(_CALLER, "voice")
    await agent.end_voice_session(session.session_id)  # the hangup persists, then drops the session
    assert await session_manager.get_or_create(_CALLER, "voice") is not session  # precondition: from storage
    [c async for c in agent.stream_voice_turn(_CALLER, "voice", "Thanks")]
    turn_3 = llm.requests[3]["input"]
    assert [i["id"] for i in turn_3 if i.get("type") == "reasoning"] == ["rs_1", "rs_2", "rs_3"]


async def test_a_voice_step_cut_off_before_any_text_is_retried_and_its_reasoning_is_not_kept(
    openai_settings, session_manager, cost_tracker
):
    """Reasoning can use up the voice request's small token cap: the response is `incomplete`."""
    cut = Incomplete([reasoning_item("rs_cut", "ENC-CUT")])
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [cut, _SECOND])
    chunks = [c async for c in agent.stream_voice_turn(_CALLER, "voice", "Which tea?")]

    assert llm.paths == ["/v1/responses"] * 2  # precondition: one retry
    assert llm.requests[0]["max_output_tokens"] == 220
    assert llm.requests[1]["max_output_tokens"] > 220
    assert "ENC-CUT" not in str(llm.requests[1])  # the cut-off response is dropped, not replayed
    assert chunks[-1].content == "Green tea."


async def test_a_voice_tool_call_cut_off_in_its_arguments_never_runs(openai_settings, session_manager, cost_tracker):
    """The arguments stop mid-JSON: the response is `incomplete`, which must read as truncated, or the call
    would run with empty arguments. Nothing was spoken, so the turn retries once at a bigger limit first."""
    cut = Incomplete([function_call_item("lookup", '{"q": "te', "call_1", "fc_1")])
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [cut, cut])
    with (
        patch.object(agent._tools, "execute", wraps=agent._tools.execute) as execute,
        pytest.raises(LLMOutputTruncatedError),
    ):
        [c async for c in agent.stream_voice_turn(_CALLER, "voice", "Which tea?")]

    assert llm.paths == ["/v1/responses"] * 2  # precondition: the retry happened
    execute.assert_not_awaited()
    session = await session_manager.get_or_create(_CALLER, "voice")
    assert [m.role for m in session.messages] == [MessageRole.USER]


# ── A turn closed mid-reply leaves no half-built message ─────────────────────


def _text_then_hang() -> RawStream:
    """A reply that streams its first text and then goes on generating."""
    events = responses_events(_SECOND)
    return RawStream(
        events[: next(i for i, e in enumerate(events) if e["type"] == "response.output_text.delta") + 1], hang=True
    )


async def test_a_voice_turn_closed_mid_reply_leaves_the_user_message_and_no_assistant_message(
    openai_settings, session_manager, cost_tracker
):
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_text_then_hang()])
    stream = agent.stream_voice_turn(_CALLER, "voice", "Which tea?")

    async def until_the_first_text() -> None:
        async for chunk in stream:
            if chunk.type is StreamEventType.TEXT:
                return  # the caller hung up while the model was speaking

    await asyncio.wait_for(until_the_first_text(), timeout=5)
    assert not llm.streams[0].closed  # precondition: the reply was still being generated
    await stream.aclose()

    session = await session_manager.get_or_create(_CALLER, "voice")
    assert [m.role for m in session.messages] == [MessageRole.USER]


async def test_a_voice_turn_cancelled_mid_reply_closes_the_http_stream_and_leaves_no_assistant_message(
    openai_settings, session_manager, cost_tracker
):
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_text_then_hang()])
    got_text = asyncio.Event()

    async def read() -> None:
        async for chunk in agent.stream_voice_turn(_CALLER, "voice", "Which tea?"):
            if chunk.type is StreamEventType.TEXT:
                got_text.set()

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(got_text.wait(), timeout=5)
        assert not llm.streams[0].closed  # precondition: the reply was still being generated
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert llm.streams[0].closed
    session = await session_manager.get_or_create(_CALLER, "voice")
    assert [m.role for m in session.messages] == [MessageRole.USER]


async def test_a_web_chat_turn_cancelled_mid_reply_leaves_no_assistant_message(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete(_USER, "web", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_text_then_hang()])
    got_text = asyncio.Event()

    async def read() -> None:
        async for chunk in agent.handle_message_stream(_USER, "web", "Which tea?", memory=False):
            if chunk.type is StreamEventType.TEXT:
                got_text.set()

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(got_text.wait(), timeout=5)
        assert not llm.streams[0].closed  # precondition
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert llm.streams[0].closed
    session = await session_manager.get_or_create(_USER, "web")
    assert not [m for m in session.messages if m.role is MessageRole.ASSISTANT]


# ── run_headless ─────────────────────────────────────────────────────────────


async def test_run_headless_sends_a_steps_reasoning_on_its_next_step_and_persists_nothing(
    openai_settings, session_manager, cost_tracker
):
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND])
    answer = await agent.run_headless("Which tea?", user_id="usr1", channel="scheduled")

    assert answer == "Green tea."
    assert llm.paths == ["/v1/responses"] * 2  # precondition: a tool run made two requests
    assert _kinds(llm.requests[1]) == ["user", "reasoning", "function_call", "function_call_output"]
    assert llm.requests[1]["input"][1:3] == _FIRST
    stored = await session_manager._store.load(
        "scheduled:usr1", user_id="usr1", channel="scheduled", legacy_fallback=False
    )
    assert stored is None and not session_manager._cache


# ── One-shot internal calls ──────────────────────────────────────────────────


def _assert_sent_once_to_responses(llm: ScriptedOpenAI, needle: str) -> None:
    """One non-streamed `/responses` request, stateless, that carries ``needle`` in its input."""
    assert llm.paths == ["/v1/responses"]
    body = llm.requests[0]
    assert body["store"] is False and "stream" not in body
    assert needle in json.dumps(body["input"])


async def test_the_client_summary_of_a_long_conversation_goes_to_responses(
    openai_settings, session_manager, cost_tracker
):
    session = await session_manager.get_or_create(_USER, "test")
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(session, LLMMessage(role=role, content=f"message {index} " + "x" * 40_000))
    agent_llm = ScriptedOpenAI(openai_settings, [[message_item("Reply.")]], prompt_tokens=100_000)
    summary_llm = ScriptedOpenAI(openai_settings, [[message_item("THE-SUMMARY")]])
    compactor = Compactor(
        summary_llm.provider,
        None,
        cost_tracker,
        session_manager,
        None,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
    )
    agent = Agent(
        openai_settings, agent_llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor
    )
    await session_manager.onboarding.complete(_USER, "test", now=1.0)
    await agent.handle_message(_USER, "test", "turn", memory=False)
    await agent.wait_compactions()

    assert agent_llm.paths == ["/v1/responses"]  # precondition: the turn itself
    _assert_sent_once_to_responses(summary_llm, "Summarize this conversation")
    assert session.messages[0].content == "[Previous conversation summary]\nTHE-SUMMARY"  # the summary was used


async def test_the_onboarding_extraction_goes_to_responses(openai_settings, session_manager, cost_tracker):
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [[message_item("{}")]])
    await agent._ingest_onboarding_reply("I'm Bob", _USER, "web", _Onboarding(prompt=False, ingest=True))

    _assert_sent_once_to_responses(llm, "I'm Bob")


async def test_the_voice_call_outcome_goes_to_responses(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [[message_item("{}")]])
    await extract_outcome(llm.provider, "caller: please book the dentist", "", language="en")

    _assert_sent_once_to_responses(llm, "please book the dentist")


async def test_the_voice_thread_summary_goes_to_responses(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [[message_item('{"summary": "Booked."}')]])
    outcome = SimpleNamespace(task_result="booked", to_json=lambda: '{"task_result": "booked-by-phone"}')
    summary, _satisfied, failed = await merge_summary(llm.provider, "", outcome, [], "en")

    _assert_sent_once_to_responses(llm, "booked-by-phone")
    assert (summary, failed) == ("Booked.", False)


async def test_an_mcp_sampling_request_goes_to_responses(openai_settings, cost_tracker):
    """Production builds this backend with an Anthropic provider only; it takes any provider."""
    llm = ScriptedOpenAI(openai_settings, [[message_item("sampled")]])
    backend = StandaloneLLMBackend(llm.provider, cost_tracker, "gpt-5.2", {})
    reply = await backend.complete([{"role": "user", "content": "sample this"}])

    _assert_sent_once_to_responses(llm, "sample this")
    assert reply == "sampled"


# ── A history that crosses between the two APIs ──────────────────────────────


def _on_chat_completions(settings):
    return settings.model_copy(update={"responses_api_providers": []})


async def test_a_history_saved_with_reasoning_is_rebuilt_on_an_endpoint_that_stays_on_chat_completions(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete(_USER, "test", now=1.0)
    saver, saved_with = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND])
    await saver.handle_message(_USER, "test", "Which tea?", memory=False)
    chat, on_chat = _agent(_on_chat_completions(openai_settings), session_manager, cost_tracker, ["Streamed."])
    [c async for c in chat.handle_message_stream(_USER, "test", "And coffee?", memory=False)]

    assert saved_with.paths == ["/v1/responses"] * 2  # precondition: the history holds Responses items
    assert on_chat.paths == ["/v1/chat/completions"]
    body = on_chat.requests[0]
    assert body["stream"] is True
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert body["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert "ENC-" not in json.dumps(body)


async def test_a_history_saved_by_a_chat_completions_stream_is_rebuilt_on_the_responses_api(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete(_USER, "test", now=1.0)
    chat, on_chat = _agent(
        _on_chat_completions(openai_settings), session_manager, cost_tracker, ["call:lookup", "Green tea."]
    )
    [c async for c in chat.handle_message_stream(_USER, "test", "Which tea?", memory=False)]
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_THIRD])
    [c async for c in agent.handle_message_stream(_USER, "test", "And coffee?", memory=False)]

    assert on_chat.paths == ["/v1/chat/completions"] * 2  # precondition: saved by the Chat path
    assert llm.paths == ["/v1/responses"]
    sent = llm.requests[0]["input"]
    assert _kinds(llm.requests[0]) == ["user", "function_call", "function_call_output", "assistant", "user"]
    assert sent[1:4] == [
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "found "},
        {"role": "assistant", "content": "Green tea."},
    ]
    saved = (await session_manager.get_or_create(_USER, "test")).messages
    assert [bool(m.raw) for m in saved] == [False, False, False, False, False, True]  # only the new reply has any
