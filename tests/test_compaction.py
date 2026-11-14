"""History compaction by prompt tokens (issue #357 slice 9, owner ruling Q20)."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import warnings
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from anthropic_sse import ScriptedAnthropic, text
from openai import AsyncOpenAI
from pydantic import SecretStr

from pincer.core.agent import Agent, StreamEventType, _sanitize_tool_pairs
from pincer.core.compaction import Compactor, LastRequest, choose_cut
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.tools.registry import ToolRegistry

_BIG = 40_000  # characters per earlier message
_SUMMARY = "SUMMARY-OF-THE-EARLIER-TALK"


def _earlier(index: int) -> str:
    head = f"message {index} "
    return head + "x" * (_BIG - len(head))


async def _seed(session_manager, user_id: str, channel: str, count: int = 10) -> None:
    """`count` earlier messages of 40,000 characters, alternating, user first."""
    session = await session_manager.get_or_create(user_id, channel)
    for index in range(count):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(session, LLMMessage(role=role, content=_earlier(index)))


def _reply(content: str, reads: int) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    """A text reply whose request reports `reads` cache-read tokens (plus 10 uncached)."""
    return [text(content)], "end_turn", {"cache_read_input_tokens": reads}


def _compactor(summary_llm, cost_tracker, session_manager, memory=None, **overrides: Any) -> Compactor:
    options: dict[str, Any] = {
        "provider": "anthropic",
        "is_free": False,
        "at_tokens": 80_000,
        "to_tokens": 50_000,
    }
    options.update(overrides)
    return Compactor(summary_llm, None, cost_tracker, session_manager, memory, **options)


async def test_a_turn_over_the_limit_compacts_the_oldest_messages_into_a_summary(
    settings, session_manager, cost_tracker
):
    await _seed(session_manager, "user1", "test")
    turns = [_reply("Reply 1", 70_000), _reply("Reply 2", 100_000)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()
        session = await session_manager.get_or_create("user1", "test")
        assert len(session.messages) == 12  # 70K is under the limit: nothing changed
        assert summary_llm.requests == []

        await agent.handle_message("user1", "test", "turn 2", memory=False)
        await agent.wait_compactions()

    assert len(session.messages) == 9
    assert session.messages[0].role == MessageRole.SYSTEM
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[1].role == MessageRole.USER
    assert session.messages[1].content.startswith("message 6 ")
    assert [m.content for m in session.messages[-4:]] == ["turn 1", "Reply 1", "turn 2", "Reply 2"]
    stored = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert stored is not None
    assert stored["messages"].count('"role"') == 9


async def _compacted_session(settings, session_manager, cost_tracker, agent_llm, summary_llm) -> Agent:
    """Two turns over 10 big earlier messages; the second (100K) compacts them to the summary + message 6 on."""
    await _seed(session_manager, "user1", "test")
    compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
    agent = Agent(settings, agent_llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
    for turn in ("turn 1", "turn 2"):
        await agent.handle_message("user1", "test", turn, memory=False)
        await agent.wait_compactions()
    return agent


async def test_claude_receives_the_summary_as_a_message_unchanged_until_the_next_compaction(
    settings, session_manager, cost_tracker
):
    turns = [_reply("Reply 1", 70_000), _reply("Reply 2", 100_000), _reply("Reply 3", 1), _reply("Reply 4", 1)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        agent = await _compacted_session(settings, session_manager, cost_tracker, llm, summary_llm)
        await agent.handle_message("user1", "test", "turn 3", memory=False)
        await agent.handle_message("user1", "test", "turn 4", memory=False)

    first = llm.requests[2]["messages"][0]
    assert first == {
        "role": "user",
        "content": [
            {"type": "text", "text": f"[Previous conversation summary]\n{_SUMMARY}"},
            {"type": "text", "text": _earlier(6)},
        ],
    }
    assert llm.requests[3]["messages"][0] == first
    assert _SUMMARY not in str(llm.requests[2].get("system"))


class _ScriptedOpenAI:
    """An OpenAI provider whose completions are scripted: (text, prompt_tokens, cached_tokens) each."""

    def __init__(self, settings, replies: list[tuple[str, int, int]]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.provider = OpenAICompatibleProvider(
            settings.model_copy(
                update={
                    "openai_model": "gpt-4o",
                    "openai_api_key": SecretStr("sk-test"),
                    "responses_api_providers": [],  # this test is about Chat Completions
                }
            ),
            "openai",
        )
        self.provider._client = AsyncOpenAI(
            api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        content, prompt, cached = self._replies.pop(0)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": 5,
                    "total_tokens": prompt + 5,
                    "prompt_tokens_details": {"cached_tokens": cached},
                },
            },
        )


async def test_openai_receives_the_summary_as_a_system_message_after_the_system_prompt(
    settings, session_manager, cost_tracker
):
    await _seed(session_manager, "user1", "test")
    llm = _ScriptedOpenAI(settings, [("Reply 1", 70_010, 70_000), ("Reply 2", 100_010, 100_000), ("Reply 3", 10, 0)])
    async with ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm:
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        for turn in ("turn 1", "turn 2", "turn 3"):
            await agent.handle_message("user1", "test", turn, memory=False)
            await agent.wait_compactions()
    await llm.provider.close()

    sent = llm.requests[2]["messages"]
    assert sent[0]["role"] == "system"
    assert _SUMMARY not in sent[0]["content"]
    assert sent[1] == {"role": "system", "content": f"[Previous conversation summary]\n{_SUMMARY}"}
    assert sent[2] == {"role": "user", "content": _earlier(6)}


async def test_under_or_at_the_limit_the_history_is_never_changed_between_turns(
    settings, session_manager, cost_tracker
):
    await _seed(session_manager, "user1", "test")
    # 10 uncached + 79,990 read = exactly 80,000: not over the limit
    turns = [_reply(f"Reply {n}", 79_990) for n in range(1, 4)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, []) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        for n in range(1, 4):
            await agent.handle_message("user1", "test", f"turn {n}", memory=False)
            await agent.wait_compactions()

    assert summary_llm.requests == []
    for earlier, later in zip(llm.requests, llm.requests[1:], strict=False):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) == 16


def _sized(role: MessageRole, **kwargs: Any) -> LLMMessage:
    return LLMMessage(role=role, content="y" * 1000, **kwargs)


def test_a_cut_that_lands_inside_a_tool_pair_moves_past_its_results():
    calls = [ToolCall(id="a", name="search", arguments={}), ToolCall(id="b", name="search", arguments={})]
    messages = [
        _sized(MessageRole.USER),
        _sized(MessageRole.ASSISTANT, tool_calls=calls),
        _sized(MessageRole.TOOL_RESULT, tool_call_id="a"),
        _sized(MessageRole.TOOL_RESULT, tool_call_id="b"),
        _sized(MessageRole.ASSISTANT),
        _sized(MessageRole.USER),
        _sized(MessageRole.ASSISTANT),
        _sized(MessageRole.USER),
    ]
    last = LastRequest.measure(8_000, messages, overhead_chars=0)
    # About 1,500 tokens must go: the oldest two messages, which would end the cut on a tool result
    cut = choose_cut(messages, last, target_tokens=8_000 + 2048 - 1_500)

    assert cut == 4
    assert messages[cut].role == MessageRole.ASSISTANT and not messages[cut].tool_calls
    assert _sanitize_tool_pairs(messages[cut:]) == messages[cut:]
    assert _sanitize_tool_pairs(messages[:cut]) == messages[:cut]  # the pair goes whole


@pytest.mark.parametrize(("reads", "compacted"), [(40_001, True), (0, False)])
async def test_the_trigger_counts_cache_writes_and_reads(settings, session_manager, cost_tracker, reads, compacted):
    await _seed(session_manager, "user1", "test")
    usage = {"cache_creation_input_tokens": 40_000, "cache_read_input_tokens": reads}  # + 10 uncached
    async with (
        ScriptedAnthropic(settings, [([text("Reply 1")], "end_turn", usage)]) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert (session.messages[0].role == MessageRole.SYSTEM) is compacted
    assert len(summary_llm.requests) == (1 if compacted else 0)


async def test_the_summary_call_is_recorded_in_the_cost_log(settings, session_manager, cost_tracker):
    turns = [_reply("Reply 1", 70_000), _reply("Reply 2", 100_000)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        await _compacted_session(settings, session_manager, cost_tracker, llm, summary_llm)

    summary = await cost_tracker.get_summary()
    assert summary.total_calls == 3  # the two turns and the summary
    assert summary.total_cache_read_tokens == 170_000  # the turns'; the summary call read none
    assert summary.total_input_tokens == 30


class _Memories:
    """A memory store that records what it is asked to keep."""

    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    async def store_memory(self, **kwargs: Any) -> str:
        self.stored.append(kwargs)
        return "mem-1"


@pytest.mark.parametrize("memory", [True, False])
async def test_the_summary_is_a_memory_only_with_memory_on(settings, session_manager, cost_tracker, memory):
    await _seed(session_manager, "user1", "test")
    memories = _Memories()
    turns = [_reply("Reply 1", 100_000)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager, memories)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=memory)
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"  # compacted either way
    expected = [{"user_id": "user1", "content": _SUMMARY, "category": "conversation_summary"}] if memory else []
    assert memories.stored == expected


async def test_a_failed_summary_keeps_the_history_and_saves_nothing(settings, session_manager, cost_tracker, caplog):
    turns = [_reply("Reply 1", 70_000), _reply("Reply 2", 100_000)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [400]) as summary_llm,
    ):
        await _compacted_session(settings, session_manager, cost_tracker, llm, summary_llm)

    assert len(summary_llm.requests) == 1  # precondition: the summary was asked for, and failed
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) == 14
    assert session.messages[0].content == _earlier(0)
    stored = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert stored is not None
    assert stored["messages"].count('"role"') == 14
    assert any(
        "no summary, history kept; compaction will be retried after a later turn" in r.getMessage()
        for r in caplog.records
    )


async def test_a_later_turn_over_the_limit_retries_the_compaction(settings, session_manager, cost_tracker):
    turns = [_reply("Reply 1", 70_000), _reply("Reply 2", 100_000), _reply("Reply 3", 100_000)]
    async with (
        ScriptedAnthropic(settings, turns) as llm,
        ScriptedAnthropic(settings, [400, ([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        agent = await _compacted_session(settings, session_manager, cost_tracker, llm, summary_llm)
        await agent.handle_message("user1", "test", "turn 3", memory=False)
        await agent.wait_compactions()

    assert len(summary_llm.requests) == 2
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) == 11
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[1].content == _earlier(6)


async def test_a_long_session_is_never_cut_by_message_count(tmp_path):
    from pincer.core.session import SessionManager

    manager = SessionManager(tmp_path / "test.db")
    await manager.initialize()
    try:
        session = await manager.get_or_create("user1", "test")
        for index in range(200):  # four times the old cap of 50: no cut at any length
            role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            await manager.add_message(session, LLMMessage(role=role, content=f"message {index}"))
        assert len(session.messages) == 200
    finally:
        await manager.close()

    reloaded = SessionManager(tmp_path / "test.db")
    await reloaded.initialize()
    try:
        stored = await reloaded.get_or_create("user1", "test")
    finally:
        await reloaded.close()
    assert [m.content for m in stored.messages] == [f"message {i}" for i in range(200)]


class _GatedSummary:
    """A summary model that answers only once released, so the test can act while it runs."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return LLMResponse(
            content=_SUMMARY,
            provider="anthropic",
            request_model="claude-haiku-4-5-20251001",
            input_tokens=10,
            output_tokens=5,
        )


async def test_a_turn_during_the_summary_runs_on_the_full_history_and_survives_the_swap(
    settings, session_manager, cost_tracker
):
    await _seed(session_manager, "user1", "test")
    gated = _GatedSummary()
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _reply("Reply 2", 1)]) as llm:
        agent = Agent(
            settings,
            llm.provider,
            session_manager,
            cost_tracker,
            ToolRegistry(),
            compactor=_compactor(gated, cost_tracker, session_manager),
        )
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await asyncio.wait_for(gated.started.wait(), timeout=5)
        # Not blocked by the summary call
        await asyncio.wait_for(agent.handle_message("user1", "test", "turn 2", memory=False), timeout=5)
        gated.release.set()
        await asyncio.wait_for(agent.wait_compactions(), timeout=5)

    assert llm.requests[1]["messages"][0]["content"] == _earlier(0)  # turn 2 sent the full history
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[1].content == _earlier(6)
    assert [m.content for m in session.messages[-4:]] == ["turn 1", "Reply 1", "turn 2", "Reply 2"]
    assert len(session.messages) == 9


async def test_a_history_changed_during_the_summary_discards_it(settings, session_manager, cost_tracker, caplog):
    await _seed(session_manager, "user1", "test")
    caplog.set_level(logging.INFO, logger="pincer.core.compaction")
    gated = _GatedSummary()
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm:
        agent = Agent(
            settings,
            llm.provider,
            session_manager,
            cost_tracker,
            ToolRegistry(),
            compactor=_compactor(gated, cost_tracker, session_manager),
        )
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await asyncio.wait_for(gated.started.wait(), timeout=5)
        session = await session_manager.get_or_create("user1", "test")
        # As a tool-pair repair would: the oldest message is a new object now
        edited = [LLMMessage(role=MessageRole.USER, content="edited"), *session.messages[1:]]
        session.messages = edited
        gated.release.set()
        await asyncio.wait_for(agent.wait_compactions(), timeout=5)

    assert session.messages == edited
    assert any("compaction discarded" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", ["stream", "voice"])
async def test_streamed_and_voice_turns_compact_after_the_reply_even_when_the_reader_stops_at_done(
    settings, session_manager, cost_tracker, path
):
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    await _seed(session_manager, "user1", path)
    # The streamed chat makes a tool-loop request, then streams its final reply
    script = [_reply("Reply 1", 100_000)] * (2 if path == "stream" else 1)
    async with (
        ScriptedAnthropic(settings, script) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        if path == "stream":
            stream = agent.handle_message_stream("user1", path, "turn 1", memory=False)
        else:
            stream = agent.stream_voice_turn("user1", path, "turn 1")
        async with aclosing(stream):
            async for chunk in stream:
                if chunk.type == StreamEventType.DONE:
                    assert summary_llm.requests == []  # nothing before the reply is out
                    break
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", path)
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[-2].content == "turn 1"
    assert session.messages[-1].content == "Reply 1"


async def test_with_the_budget_spent_the_history_is_kept_without_a_summary_call(settings, session_manager, tmp_path):
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    # A budget the turn's own call crosses: it may run (nothing spent yet), and it is recorded
    costs = CostTracker(tmp_path / "budget.db", daily_budget=1e-9, prices=load_price_table())
    await costs.initialize()
    await _seed(session_manager, "user1", "test")
    try:
        async with (
            ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm,
            ScriptedAnthropic(settings, []) as summary_llm,
        ):
            compactor = _compactor(summary_llm.provider, costs, session_manager)
            agent = Agent(settings, llm.provider, session_manager, costs, ToolRegistry(), compactor=compactor)
            await agent.handle_message("user1", "test", "turn 1", memory=False)
            assert await costs.budget_reached()  # precondition: the turn's call reached the budget
            await agent.wait_compactions()
    finally:
        await costs.close()

    assert summary_llm.requests == []
    session = await session_manager.get_or_create("user1", "test")
    assert [m.content for m in session.messages] == [_earlier(i) for i in range(10)] + ["turn 1", "Reply 1"]


def test_the_compaction_target_must_be_below_its_limit():
    from pydantic import ValidationError

    from pincer.config.tools import ToolSettings

    defaults = ToolSettings()
    assert (defaults.compact_at_tokens, defaults.compact_to_tokens) == (80_000, 50_000)
    with pytest.raises(ValidationError, match=r"compact_to_tokens \(80000\) must be below compact_at_tokens \(80000\)"):
        ToolSettings(compact_at_tokens=80_000, compact_to_tokens=80_000)
    with pytest.raises(ValidationError, match="greater than or equal to 1000"):
        ToolSettings(compact_at_tokens=0)


class _Router:
    def __init__(self, provider) -> None:
        self._provider = provider

    def get_summarizer(self):
        return self._provider, "claude-haiku-4-5-20251001"

    def is_free(self, name: str) -> bool:
        return False


@pytest.mark.parametrize(("threshold", "warned"), [(20, True), (None, False)])
async def test_a_summary_threshold_still_set_is_warned_about_once_at_startup(
    settings, session_manager, cost_tracker, caplog, threshold, warned
):
    from pincer.core.compaction import build_compactor

    settings.summary_threshold = threshold
    async with ScriptedAnthropic(settings, []) as summary_llm:
        compactor = build_compactor(settings, _Router(summary_llm.provider), cost_tracker, session_manager, None)

    assert isinstance(compactor, Compactor)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    expected = (
        "PINCER_SUMMARY_THRESHOLD no longer has an effect: history is compacted by prompt tokens "
        "(PINCER_COMPACT_AT_TOKENS / PINCER_COMPACT_TO_TOKENS)"
    )
    assert warnings == ([expected] if warned else [])


async def test_a_memory_store_error_is_logged_and_the_compaction_still_applies(
    settings, session_manager, cost_tracker, caplog
):
    class _FailingMemories:
        async def store_memory(self, **_kwargs: Any) -> str:
            raise RuntimeError("embedding failed")

    await _seed(session_manager, "user1", "test")
    async with (
        ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        compactor = _compactor(summary_llm.provider, cost_tracker, session_manager, _FailingMemories())
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        result = await agent.handle_message("user1", "test", "turn 1", memory=True)
        await agent.wait_compactions()

    assert result.text.startswith("Reply 1")
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    failures = [r for r in caplog.records if "as a memory failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].exc_info is not None


async def test_shutdown_cancels_a_pending_compaction_and_leaves_the_session_as_it_was(
    settings, session_manager, cost_tracker, caplog
):
    await _seed(session_manager, "user1", "test")
    gated = _GatedSummary()
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm:
        agent = Agent(
            settings,
            llm.provider,
            session_manager,
            cost_tracker,
            ToolRegistry(),
            compactor=_compactor(gated, cost_tracker, session_manager),
        )
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await asyncio.wait_for(gated.started.wait(), timeout=5)  # precondition: the summary is running
        session = await session_manager.get_or_create("user1", "test")
        before = list(session.messages)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await agent.cancel_compactions()
            await asyncio.wait_for(agent.wait_compactions(), timeout=1)  # nothing left running
            gc.collect()

    assert session.messages == before
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
    assert [r.getMessage() for r in caplog.records if "Task was destroyed" in r.getMessage()] == []


def test_the_api_lifespan_cancels_the_agents_compactions(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    get_settings_relaxed.cache_clear()
    app = create_app()
    agent = MagicMock(spec=Agent)
    agent.cancel_compactions = AsyncMock()
    app.state.agent = agent
    try:
        with TestClient(app):
            agent.cancel_compactions.assert_not_awaited()  # only at shutdown
    finally:
        get_settings_relaxed.cache_clear()

    agent.cancel_compactions.assert_awaited_once()


async def test_the_rate_is_measured_on_what_the_request_sent(settings, cost_tracker, tmp_path):
    """The reviewer's case (PR #366) was a trim between the request and the compaction. Nothing trims a
    turn's history any more; the tokens-per-character rate still comes from the 51 messages the
    request sent, and the cut is chosen on the 52 the session holds after the reply."""
    from pincer.core.session import SessionManager

    sessions = SessionManager(tmp_path / "long.db")
    await sessions.initialize()
    try:
        session = await sessions.get_or_create("user1", "test")
        for index in range(50):
            role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            head = f"message {index} "
            await sessions.add_message(session, LLMMessage(role=role, content=head + "x" * (10_000 - len(head))))
        # 10 uncached + 110,450 read = 110,460: 0.22 tokens per character of what was sent
        async with (
            ScriptedAnthropic(settings, [_reply("Reply 1", 110_450)]) as llm,
            ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
        ):
            compactor = _compactor(summary_llm.provider, cost_tracker, sessions)
            agent = Agent(settings, llm.provider, sessions, cost_tracker, ToolRegistry(), compactor=compactor)
            await agent.handle_message("user1", "test", "turn 1", memory=False)
            await agent.wait_compactions()
        assert len(llm.requests[0]["messages"]) == 51  # precondition: the request carried all 51
    finally:
        await sessions.close()

    assert len(session.messages) == 24  # the summary + the newest 23 of the 52: 29 summarized
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[1].content.startswith("message 29 ")


def test_the_rate_counts_the_thinking_a_request_replayed():
    """A request resends the thinking the history keeps: its tokens are in the prompt, so its
    characters must be in the calibration."""
    thought = {"type": "thinking", "thinking": "t" * 3_000, "signature": "sig"}
    sent = [
        LLMMessage(role=MessageRole.USER, content="u" * 1_000),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 1_000, raw=[thought], wire="anthropic:x"),
    ]

    last = LastRequest.measure(5_000, sent, overhead_chars=0)

    assert last.tokens_per_char == 5_000 / 5_003  # 1,000 + 1,000 + 3,000 characters and the 3-character signature


# ── what a request carries, beyond text: the size estimate behind every cut (PR #379 review) ─────────


def _rate(prompt_tokens: int, *messages: LLMMessage) -> float:
    return LastRequest.measure(prompt_tokens, list(messages), overhead_chars=0).tokens_per_char


def test_a_photo_counts_as_the_characters_of_its_tokens():
    from pincer.core.compaction import PHOTO_CHARS
    from pincer.llm.base import ImageContent

    photo = LLMMessage(
        role=MessageRole.USER, content="u" * 1_000, images=[ImageContent.from_bytes(b"jpeg bytes", "image/jpeg")]
    )

    assert PHOTO_CHARS == 16_000  # an estimate (a photo at the 2,576 px cap is about 4,600 tokens)
    assert _rate(17_000, photo) == 1.0  # 1,000 characters of text + one photo


def test_a_compacted_openai_window_counts_its_encrypted_item_and_the_messages_it_keeps():
    from pincer.llm.openai_responses import WINDOW_NOTICE

    window = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "w" * 2_000}]},
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "E" * 40_000},
    ]
    message = LLMMessage(role=MessageRole.SYSTEM, content=WINDOW_NOTICE, raw=window, wire="openai:gpt-6")

    assert _rate(42_000, message) == 1.0  # not the notice's few characters


def test_a_compaction_block_counts_its_content_and_signature():
    block = {"type": "compaction", "content": "S" * 5_000, "signature": "G" * 3_000}
    message = LLMMessage(role=MessageRole.SYSTEM, content="[summary]\n" + "S" * 5_000, raw=[block], wire="anthropic:x")

    assert _rate(8_000, message) == 1.0


def test_encrypted_reasoning_counts_but_the_text_and_calls_of_the_raw_output_do_not_count_twice():
    answer = [{"type": "output_text", "text": "a" * 1_000}]
    raw = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "R" * 5_000},
        {"type": "message", "role": "assistant", "content": answer},
        {"type": "function_call", "call_id": "c1", "name": "search", "arguments": "{}"},
    ]
    call = ToolCall(id="c1", name="search", arguments={})
    message = LLMMessage(role=MessageRole.ASSISTANT, content="a" * 1_000, tool_calls=[call], raw=raw, wire="openai:x")
    calls = len(json.dumps([call.to_dict()]))

    assert _rate(1_000 + calls + 5_000, message) == 1.0


def test_an_anthropic_assistant_message_counts_its_thinking_once_beside_its_text_and_tool_calls():
    call = ToolCall(id="toolu_1", name="search", arguments={"q": "x"})
    raw = [
        {"type": "thinking", "thinking": "t" * 3_000, "signature": "sig"},
        {"type": "redacted_thinking", "data": "d" * 500},
        {"type": "text", "text": "a" * 1_000},
        {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
    ]
    message = LLMMessage(
        role=MessageRole.ASSISTANT, content="a" * 1_000, tool_calls=[call], raw=raw, wire="anthropic:x"
    )
    calls = len(json.dumps([call.to_dict()]))

    assert _rate(1_000 + calls + 3_003 + 500, message) == 1.0


def test_the_cut_of_a_photo_turn_saves_what_the_photo_cost():
    """The first turn holds the photo that made the prompt big: dropping that one turn is enough."""
    from pincer.core.compaction import choose_hard_cut
    from pincer.llm.base import ImageContent

    photo = ImageContent.from_bytes(b"jpeg bytes", "image/jpeg")
    messages = [
        LLMMessage(role=MessageRole.USER, content="u" * 100, images=[photo]),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 100),
        LLMMessage(role=MessageRole.USER, content="u" * 100),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 100),
        LLMMessage(role=MessageRole.USER, content="u" * 100),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 100),
    ]
    last = LastRequest.measure(16_600, messages, overhead_chars=0)

    assert choose_hard_cut(messages, last, target_tokens=1_000) == 2  # the photo turn only
