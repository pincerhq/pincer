"""Claude compacts the conversation on the server (issue #358, slice 4)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import aclosing
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anthropic_sse import ScriptedAnthropic, compaction, text
from sqlmodel import select

from pincer.core.agent import Agent, StreamEventType
from pincer.core.compaction import (
    CompactionOutcome,
    CompactionStrategy,
    Compactor,
    LastRequest,
    SentRequest,
    ServerCompaction,
    pick_strategy,
)
from pincer.core.session import SessionManager
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.base import BaseLLMProvider, ImageContent, LLMMessage, MessageRole
from pincer.models.costs import CostLog
from pincer.tools.registry import ToolRegistry

_HAIKU = "claude-haiku-5-5"
_BIG = 40_000  # characters per earlier message
_SUMMARY = "SUMMARY-OF-THE-EARLIER-TALK"
_SIGNATURE = "SIG-compaction-9c1e"
_BLOCK = compaction(_SUMMARY, _SIGNATURE)
_THOUGHT = {"type": "thinking", "thinking": "SECRET-THOUGHT-7f3a", "signature": "SIG-7f3a-signature"}
_BETAS = "compact-2026-09-04,thinking-binding-controls-2026-08-01"
# What the API reports for a compaction: top-level usage is zero, the call is in `iterations`
_COMPACTION_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "iterations": [
        {
            "type": "compaction",
            "input_tokens": 144,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 5,
            "output_tokens": 276,
        }
    ],
}
_COMPACTED = ([_BLOCK], "compaction", _COMPACTION_USAGE)


def _earlier(index: int) -> str:
    head = f"message {index} "
    return head + "x" * (_BIG - len(head))


async def _seed(session_manager, user_id: str, channel: str, count: int = 10, photo: bytes | None = None) -> None:
    """`count` earlier messages of 40,000 characters, alternating, user first; the first holds `photo`."""
    session = await session_manager.get_or_create(user_id, channel)
    for index in range(count):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        images = [ImageContent.from_bytes(photo, "image/jpeg")] if photo and index == 0 else []
        await session_manager.add_message(session, LLMMessage(role=role, content=_earlier(index), images=images))


async def _stored(session_manager, user_id: str, channel: str) -> dict[str, Any]:
    """The session as the database holds it: its messages JSON and the shas of its photo rows."""
    row = await session_manager._store.load(
        f"{channel}:{user_id}", user_id=user_id, channel=channel, legacy_fallback=False
    )
    assert row is not None
    return {"messages": row["messages"], "photos": sorted(photo.sha256 for photo in row["images"])}


async def _cost_rows(cost_tracker) -> list[dict[str, Any]]:
    """Every recorded call, oldest first: who it was for and what it was billed."""
    async with session_scope(get_database_url(cost_tracker._db_path)) as session:
        rows = (await session.exec(select(CostLog).order_by(CostLog.timestamp))).all()
        return [
            {
                "session_id": row.session_id,
                "provider": row.provider,
                "model": row.model,
                "uncached": row.input_tokens,
                "read": row.cache_read_tokens,
                "write_5m": row.cache_write_5m_tokens,
                "write_1h": row.cache_write_1h_tokens,
                "output": row.output_tokens,
            }
            for row in rows
        ]


class _Memories:
    """A memory store that records what it is asked to keep."""

    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    async def store_memory(self, **kwargs: Any) -> str:
        self.stored.append(kwargs)
        return "mem-1"


def _reply(content: str, reads: int, *, thought: bool = True) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    """A reply (thinking first) whose request reports `reads` cache-read tokens (plus 10 uncached)."""
    blocks = [dict(_THOUGHT), text(content)] if thought else [text(content)]
    return blocks, "end_turn", {"cache_read_input_tokens": reads}


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def create_event(title: str = "") -> str:
        return "created"

    registry.register(name="create_event", description="Create a calendar event", handler=create_event)
    return registry


def _compactor(settings, llm: ScriptedAnthropic, session_manager, cost_tracker, memory=None) -> Compactor:
    server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
    return Compactor(
        None,
        None,
        cost_tracker,
        session_manager,
        memory,
        provider="anthropic",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        server=server,
    )


def _agent(settings, llm: ScriptedAnthropic, session_manager, cost_tracker, registry, memory=None) -> Agent:
    compactor = _compactor(settings, llm, session_manager, cost_tracker, memory)
    return Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)


async def test_a_turn_over_the_limit_asks_the_server_to_compact_exactly_what_the_last_request_sent(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    assert len(llm.requests) == 2
    turn, request = llm.requests
    # preconditions: the turn's own request carries the whole prompt the compaction must repeat
    assert "compaction" not in turn
    assert turn["messages"][0]["content"] == _earlier(0)
    assert turn["tools"] and turn["system"]
    assert turn["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"

    assert request["compaction"] == {"type": "summarize"}
    assert {key: value for key, value in request.items() if key != "compaction"} == turn
    assert llm.headers[1]["anthropic-beta"] == _BETAS
    assert llm.urls[1].path == "/v1/messages"
    assert llm.urls[1].params["beta"] == "true"


async def test_after_the_swap_the_next_request_starts_with_the_block_then_the_kept_turn_unchanged(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    script = [_reply("Reply 1", 100_000), _COMPACTED, _reply("Reply 2", 1)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()
        session = await session_manager.get_or_create("user1", "test")
        # precondition: the turn kept for the swap holds thinking
        assert session.messages[-1].raw[0] == _THOUGHT
        await agent.handle_message("user1", "test", "turn 2", memory=False)

    # the summarized messages are gone: the block, the reply the compaction did not see, turn 2
    assert [m.role for m in session.messages] == [MessageRole.SYSTEM, MessageRole.ASSISTANT, MessageRole.USER] + [
        MessageRole.ASSISTANT
    ]
    stored = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert stored is not None
    assert json.loads(stored["messages"])[0] == {
        "role": "system",
        "content": f"[Previous conversation summary]\n{_SUMMARY}",
        "raw": [{"type": "compaction", "content": _SUMMARY, "signature": _SIGNATURE}],
        "wire": f"anthropic:{_HAIKU}",
    }

    messages = llm.requests[2]["messages"]
    assert messages[:2] == [
        {"role": "assistant", "content": [{"type": "compaction", "content": _SUMMARY, "signature": _SIGNATURE}]},
        {"role": "assistant", "content": [_THOUGHT, {"type": "text", "text": "Reply 1"}]},
    ]
    assert [m["role"] for m in messages] == ["assistant", "assistant", "user"]
    assert llm.headers[2]["anthropic-beta"] == _BETAS


async def test_a_request_carrying_the_block_has_the_compaction_beta_even_when_it_does_not_think(
    settings, session_manager, cost_tracker, registry
):
    # the default model (Haiku 4.5) is not bound: no binding beta, no block_binding
    await _seed(session_manager, "user1", "test")
    script = [_reply("Reply 1", 100_000, thought=False), _COMPACTED, _reply("Reply 2", 1, thought=False)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()
        await agent.handle_message("user1", "test", "turn 2", memory=False)

    turn, request, next_turn = llm.requests
    assert "block_binding" not in str(turn.get("thinking"))  # precondition: nothing binds this model
    assert llm.headers[0].get("anthropic-beta") is None
    assert llm.headers[1]["anthropic-beta"] == "compact-2026-09-04"
    assert request["compaction"] == {"type": "summarize"}
    assert next_turn["messages"][0]["content"][0]["type"] == "compaction"
    assert llm.headers[2]["anthropic-beta"] == "compact-2026-09-04"
    assert llm.urls[2].params["beta"] == "true"


async def _until_requests(llm: ScriptedAnthropic, count: int) -> None:
    """Wait until the endpoint has received `count` requests."""
    async with asyncio.timeout(5):
        while len(llm.requests) < count:
            await asyncio.sleep(0.01)


async def test_a_turn_during_the_compaction_runs_on_the_full_history_and_is_kept_after_the_block(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    script = [_reply("Reply 1", 100_000), _COMPACTED, _reply("Reply 2", 1)]
    async with ScriptedAnthropic(settings, script) as llm:
        gate = llm.gates[2] = asyncio.Event()  # the compaction request is held open
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await _until_requests(llm, 2)
        session = await session_manager.get_or_create("user1", "test")
        assert len(session.messages) == 12  # precondition: nothing is swapped while the call is out

        # not blocked by the compaction: a real turn on the same session
        await asyncio.wait_for(agent.handle_message("user1", "test", "turn 2", memory=False), timeout=5)
        assert llm.requests[2]["messages"][0]["content"] == _earlier(0)  # it ran on the full history
        assert len(session.messages) == 14
        gate.set()
        await asyncio.wait_for(agent.wait_compactions(), timeout=5)

    assert [(m.role, m.content[:20]) for m in session.messages] == [
        (MessageRole.SYSTEM, "[Previous conversati"),
        (MessageRole.ASSISTANT, "Reply 1"),
        (MessageRole.USER, "turn 2"),
        (MessageRole.ASSISTANT, "Reply 2"),
    ]
    assert session.messages[1].raw[0] == _THOUGHT  # the kept turns keep their thinking


async def test_a_history_changed_during_the_compaction_discards_it_and_leaves_state_and_photos_alone(
    settings, session_manager, cost_tracker, registry, caplog
):
    caplog.set_level(logging.INFO, logger="pincer.core.compaction")
    photo = b"photo-in-the-oldest-message"
    await _seed(session_manager, "user1", "test", photo=photo)
    memories = _Memories()
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        gate = llm.gates[2] = asyncio.Event()
        agent = _agent(settings, llm, session_manager, cost_tracker, registry, memories)
        await agent.handle_message("user1", "test", "turn 1", memory=True)
        await _until_requests(llm, 2)
        session = await session_manager.get_or_create("user1", "test")
        # As a tool-pair repair would: the oldest message is a new object now
        edited = [LLMMessage(role=MessageRole.USER, content="edited"), *session.messages[1:]]
        session.messages = edited
        before = await _stored(session_manager, "user1", "test")
        assert len(before["photos"]) == 1  # precondition: the photo is stored
        gate.set()
        await asyncio.wait_for(agent.wait_compactions(), timeout=5)

    assert session.messages == edited
    assert await _stored(session_manager, "user1", "test") == before
    assert memories.stored == []
    assert [r.getMessage() for r in caplog.records if "discarded" in r.getMessage()] == [
        "Session test:user1 changed while it was being compacted; compaction discarded"
    ]


async def test_the_swap_deletes_the_photos_only_the_summarized_messages_showed(
    settings, session_manager, cost_tracker, registry
):
    old_photo, new_photo = b"photo-in-a-summarized-message", b"photo-sent-while-the-compaction-ran"
    await _seed(session_manager, "user1", "test", photo=old_photo)
    script = [_reply("Reply 1", 100_000), _COMPACTED, _reply("Reply 2", 1)]
    async with ScriptedAnthropic(settings, script) as llm:
        gate = llm.gates[2] = asyncio.Event()
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await _until_requests(llm, 2)
        await agent.handle_message("user1", "test", "turn 2", images=[(new_photo, "image/jpeg")], memory=False)
        both = sorted(hashlib.sha256(raw).hexdigest() for raw in (old_photo, new_photo))
        assert (await _stored(session_manager, "user1", "test"))["photos"] == both  # precondition
        gate.set()
        await asyncio.wait_for(agent.wait_compactions(), timeout=5)

    stored = await _stored(session_manager, "user1", "test")
    assert stored["photos"] == [hashlib.sha256(new_photo).hexdigest()]
    assert json.loads(stored["messages"])[2]["images"] == [
        {"sha256": hashlib.sha256(new_photo).hexdigest(), "media_type": "image/jpeg"}
    ]


@pytest.mark.parametrize("memory", [True, False])
async def test_the_summary_is_a_memory_only_with_memory_on(settings, session_manager, cost_tracker, registry, memory):
    await _seed(session_manager, "user1", "test")
    memories = _Memories()
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry, memories)
        await agent.handle_message("user1", "test", "turn 1", memory=memory)
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].raw == [_BLOCK]  # compacted either way
    expected = [{"user_id": "user1", "content": _SUMMARY, "category": "conversation_summary"}] if memory else []
    assert memories.stored == expected


async def test_the_compaction_is_recorded_from_its_iterations_with_the_session_id(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    turn, compaction_call = await _cost_rows(cost_tracker)
    assert (turn["uncached"], turn["read"], turn["output"]) == (10, 100_000, 5)  # precondition: the turn is recorded
    # top-level usage of the compaction response is 0/0: what was billed is in `iterations`
    assert compaction_call == {
        "session_id": "test:user1",
        "provider": "anthropic",
        "model": _HAIKU,
        "uncached": 144,
        "read": 1_000,
        "write_5m": 5,
        "write_1h": 0,
        "output": 276,
    }


async def test_with_the_budget_reached_no_compaction_call_is_made_and_nothing_changes(
    settings, session_manager, tmp_path, registry
):
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    # A budget the turn's own call crosses: it may run (nothing spent yet), and it is recorded
    costs = CostTracker(tmp_path / "budget.db", daily_budget=1e-9, prices=load_price_table())
    await costs.initialize()
    await _seed(session_manager, "user1", "test", photo=b"photo-in-the-oldest-message")
    try:
        async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm:
            agent = _agent(settings, llm, session_manager, costs, registry)
            await agent.handle_message("user1", "test", "turn 1", memory=False)
            assert await costs.budget_reached()  # precondition: the turn's call reached the budget
            before = await _stored(session_manager, "user1", "test")
            await agent.wait_compactions()
    finally:
        await costs.close()

    assert len(llm.requests) == 1  # no compaction request
    assert await _stored(session_manager, "user1", "test") == before
    assert len(before["photos"]) == 1
    session = await session_manager.get_or_create("user1", "test")
    assert [m.content for m in session.messages][-2:] == ["turn 1", "Reply 1"] and len(session.messages) == 12


def _no_summary(stop_reason: str, blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """A 200 that holds no summary; the call is still billed (reported in `iterations`)."""
    return blocks, stop_reason, _COMPACTION_USAGE


_NO_SUMMARY_LOG = (
    "Session test:user1: the server returned no summary (stop_reason=%s), history kept; "
    "compaction will be retried after a later turn"
)


@pytest.mark.parametrize(
    ("answer", "warning", "billed"),
    [
        (_no_summary("max_tokens", []), _NO_SUMMARY_LOG % "max_tokens", True),
        (_no_summary("end_turn", []), _NO_SUMMARY_LOG % "end_turn", True),
        (_no_summary("compaction", [compaction(None)]), _NO_SUMMARY_LOG % "compaction", True),
        (_no_summary("max_tokens", [_BLOCK]), _NO_SUMMARY_LOG % "max_tokens", True),
        (400, "Compacting session test:user1 on the server failed", False),
    ],
    ids=["cut off at max_tokens", "no text", "null content", "a block but not the compaction stop", "HTTP 400"],
)
async def test_without_a_summary_nothing_changes_and_a_later_turn_retries(
    settings, session_manager, cost_tracker, registry, caplog, answer, warning, billed
):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    await _seed(session_manager, "user1", "test", photo=b"photo-in-the-oldest-message")
    memories = _Memories()
    script = [_reply("Reply 1", 100_000), answer, _reply("Reply 2", 100_000), _COMPACTED]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry, memories)
        await agent.handle_message("user1", "test", "turn 1", memory=True)
        before = await _stored(session_manager, "user1", "test")
        await agent.wait_compactions()
        assert "compaction" in llm.requests[1]  # precondition: the server was asked
        assert len(before["photos"]) == 1

        assert await _stored(session_manager, "user1", "test") == before
        assert memories.stored == []
        assert len(await _cost_rows(cost_tracker)) == (2 if billed else 1)  # the call is recorded where it was billed
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [warning]

        await agent.handle_message("user1", "test", "turn 2", memory=True)  # still over the limit: tried again
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert [m.content for m in session.messages][1:] == ["Reply 2"]
    assert session.messages[0].raw == [_BLOCK]
    assert len(memories.stored) == 1


async def test_an_endpoint_that_reports_no_iterations_is_recorded_from_the_top_level_usage(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    answer = ([_BLOCK], "compaction", {"input_tokens": 300, "output_tokens": 40})
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), answer]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    _, compaction_call = await _cost_rows(cost_tracker)
    assert (compaction_call["uncached"], compaction_call["read"], compaction_call["output"]) == (300, 0, 40)


def _last_request(sent: list[LLMMessage]) -> LastRequest:
    """A last request of 100,000 prompt tokens that sent `sent`, as the agent would have noted it."""
    return LastRequest.measure(100_000, sent, 0, request=SentRequest("anthropic", _HAIKU, None, None))


async def test_a_message_that_would_follow_the_summary_with_the_same_role_means_no_compaction(
    settings, session_manager, cost_tracker
):
    # A turn that died after its request: the next kept message is a user message, which the API would
    # merge into the last summarized one (docs: the first kept message must have a different role)
    session = await session_manager.get_or_create("user1", "test")
    for role, content in [(MessageRole.USER, "one"), (MessageRole.ASSISTANT, "two"), (MessageRole.USER, "three")]:
        await session_manager.add_message(session, LLMMessage(role=role, content=content))
    last = _last_request(list(session.messages))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="four"))
    async with ScriptedAnthropic(settings, []) as llm:
        compactor = _compactor(settings, llm, session_manager, cost_tracker)
        assert last.prompt_tokens > 80_000  # precondition: it is due
        assert await compactor.compact(session, last) is CompactionOutcome.FAILED

    assert llm.requests == []  # no paid call
    assert [m.content for m in session.messages] == ["one", "two", "three", "four"]


async def test_a_turn_arriving_during_the_call_cannot_become_the_first_kept_message(
    settings, session_manager, cost_tracker, registry, caplog
):
    caplog.set_level(logging.INFO, logger="pincer.core.compaction")
    session = await session_manager.get_or_create("user1", "test")
    for role, content in [(MessageRole.USER, "one"), (MessageRole.ASSISTANT, "two"), (MessageRole.USER, "three")]:
        await session_manager.add_message(session, LLMMessage(role=role, content=content))
    last = _last_request(list(session.messages))  # nothing follows: no kept message yet
    async with ScriptedAnthropic(settings, [_COMPACTED, _reply("Reply 2", 1)]) as llm:
        gate = llm.gates[1] = asyncio.Event()
        compaction_task = asyncio.create_task(
            _compactor(settings, llm, session_manager, cost_tracker).compact(session, last)
        )
        await _until_requests(llm, 1)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await asyncio.wait_for(agent.handle_message("user1", "test", "four", memory=False), timeout=5)
        gate.set()
        assert await asyncio.wait_for(compaction_task, timeout=5) is CompactionOutcome.SKIPPED

    assert [m.content for m in session.messages] == ["one", "two", "three", "four", "Reply 2"]
    assert any("compaction discarded" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("listed", "provider", "leaf_kind", "expected"),
    [
        (None, "anthropic", "anthropic", CompactionStrategy.ANTHROPIC_SERVER),
        (["foundry"], "foundry", "anthropic", CompactionStrategy.ANTHROPIC_SERVER),
        ([], "anthropic", "anthropic", CompactionStrategy.CLIENT_SUMMARY),
        (["foundry"], "anthropic", "anthropic", CompactionStrategy.CLIENT_SUMMARY),
        (None, "anthropic", "openai", CompactionStrategy.CLIENT_SUMMARY),
        (None, "anthropic", None, CompactionStrategy.CLIENT_SUMMARY),
        (None, "openai", "openai", CompactionStrategy.CLIENT_SUMMARY),
    ],
    ids=[
        "default list",
        "a listed compatible endpoint",
        "empty list",
        "endpoint not listed",
        "a listed name served by an OpenAI-wire leaf",
        "no leaf",
        "OpenAI",
    ],
)
async def test_the_strategy_follows_the_endpoint_that_served_the_last_request(
    settings, listed, provider, leaf_kind, expected
):
    settings.server_compaction_providers = listed
    async with ScriptedAnthropic(settings, []) as llm:
        leaves = {"anthropic": llm.provider, "openai": AsyncMock(spec=BaseLLMProvider), None: None}
        assert pick_strategy(settings, provider, leaves[leaf_kind]) is expected


async def test_an_endpoint_outside_the_list_keeps_the_summary_model_exactly(
    settings, session_manager, cost_tracker, registry
):
    settings.server_compaction_providers = []
    await _seed(session_manager, "user1", "test")
    async with (
        ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm,
        ScriptedAnthropic(settings, [([text(_SUMMARY)], "end_turn")]) as summary_llm,
    ):
        server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
        compactor = Compactor(
            summary_llm.provider,
            None,
            cost_tracker,
            session_manager,
            None,
            provider="anthropic",
            is_free=False,
            at_tokens=80_000,
            to_tokens=50_000,
            server=server,
        )
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    assert len(llm.requests) == 1  # the conversation's own endpoint was not asked to compact
    assert len(summary_llm.requests) == 1
    assert "compaction" not in summary_llm.requests[0]
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].content == f"[Previous conversation summary]\n{_SUMMARY}"
    assert session.messages[0].raw == []


@pytest.mark.parametrize("path", ["stream", "voice"])
async def test_streamed_and_voice_turns_compact_what_their_last_request_sent(
    settings, session_manager, cost_tracker, registry, path
):
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    settings.default_model = _HAIKU
    settings.voice_thinking = "disabled"  # a per-call setting of voice that the compaction must repeat
    await _seed(session_manager, "user1", path)
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000, thought=False), _COMPACTED]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        if path == "stream":
            stream = agent.handle_message_stream("user1", path, "turn 1", memory=False)
        else:
            stream = agent.stream_voice_turn("user1", path, "turn 1")
        async with aclosing(stream):
            async for chunk in stream:
                if chunk.type == StreamEventType.DONE:
                    break
        await agent.wait_compactions()

    turn, request = llm.requests
    assert "compaction" not in turn  # precondition
    if path == "voice":
        assert (turn["max_tokens"], turn["thinking"]) == (220, {"type": "disabled"})
    assert request["compaction"] == {"type": "summarize"}
    # the voice turn's small max_tokens would cut the summary: the call keeps the provider's default
    assert request["max_tokens"] == settings.max_tokens
    ignored = {"compaction", "max_tokens"}
    assert {k: v for k, v in request.items() if k not in ignored} == {k: v for k, v in turn.items() if k not in ignored}
    session = await session_manager.get_or_create("user1", path)
    assert session.messages[0].raw == [_BLOCK]


async def test_after_a_restart_the_stored_block_goes_first_in_the_next_request(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    script = [_reply("Reply 1", 100_000), _COMPACTED, _reply("Reply 2", 1)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()
        await session_manager.close()  # persists and drops every cached session

        restarted = SessionManager(session_manager._db_path)
        await restarted.initialize()
        try:
            assert await restarted.get_or_create("user1", "test") is not None
            agent = _agent(settings, llm, restarted, cost_tracker, registry)
            await agent.handle_message("user1", "test", "turn 2", memory=False)
        finally:
            await restarted.close()

    assert llm.requests[2]["messages"][0] == {"role": "assistant", "content": [dict(_BLOCK)]}
    assert llm.requests[2]["messages"][1] == {"role": "assistant", "content": [_THOUGHT, text("Reply 1")]}
    assert llm.headers[2]["anthropic-beta"] == _BETAS


async def test_another_model_gets_the_summary_as_text_and_no_block(settings, session_manager, cost_tracker, registry):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    sonnet = settings.model_copy(update={"default_model": "claude-sonnet-5-5"})
    async with ScriptedAnthropic(sonnet, [_reply("Reply 2", 1)]) as other:
        await Agent(sonnet, other.provider, session_manager, cost_tracker, registry).handle_message(
            "user1", "test", "turn 2", memory=False
        )

    messages = other.requests[0]["messages"]
    assert "compaction" not in json.dumps(messages)
    assert messages[0] == {
        "role": "user",
        "content": [{"type": "text", "text": f"[Previous conversation summary]\n{_SUMMARY}"}],
    }
    assert "compact-2026-09-04" not in other.headers[0]["anthropic-beta"]  # only the binding beta


async def test_compacting_again_sends_the_old_block_first_and_replaces_it(
    settings, session_manager, cost_tracker, registry
):
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    second = compaction("SECOND-SUMMARY", "SIG-second")
    script = [
        _reply("Reply 1", 100_000),
        _COMPACTED,
        _reply("Reply 2", 100_000),
        ([second], "compaction", _COMPACTION_USAGE),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        for turn in ("turn 1", "turn 2"):
            await agent.handle_message("user1", "test", turn, memory=False)
            await agent.wait_compactions()

    request = llm.requests[3]
    assert request["compaction"] == {"type": "summarize"}
    # the new block summarizes the old one and everything after it
    assert request["messages"][0] == {"role": "assistant", "content": [dict(_BLOCK)]}
    assert [m["role"] for m in request["messages"]] == ["assistant", "assistant", "user"]
    assert llm.headers[3]["anthropic-beta"] == _BETAS
    session = await session_manager.get_or_create("user1", "test")
    assert sum(m.raw[0]["type"] == "compaction" for m in session.messages if m.raw) == 1  # exactly one block
    assert [(m.role, m.content) for m in session.messages] == [
        (MessageRole.SYSTEM, "[Previous conversation summary]\nSECOND-SUMMARY"),
        (MessageRole.ASSISTANT, "Reply 2"),
    ]
    assert session.messages[0].raw == [second]


async def test_documents_today_a_stored_block_still_goes_out_when_the_endpoint_leaves_the_list(
    settings, session_manager, cost_tracker, registry
):
    """Handed to slice 10 (see the plan): the block replays whenever its wire matches and carries the compact beta
    whatever `server_compaction_providers` says. An owner who removes the endpoint from the list while a block is
    stored sends a block to an endpoint told not to use betas."""
    settings.default_model = _HAIKU
    await _seed(session_manager, "user1", "test")
    script = [_reply("Reply 1", 100_000), _COMPACTED, _reply("Reply 2", 1)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = _agent(settings, llm, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()
        settings.server_compaction_providers = []
        await agent.handle_message("user1", "test", "turn 2", memory=False)

    request = llm.requests[2]
    assert "block_binding" not in json.dumps(request.get("thinking"))  # precondition: the endpoint is off the list
    assert request["messages"][0] == {"role": "assistant", "content": [dict(_BLOCK)]}
    assert llm.headers[2]["anthropic-beta"] == "compact-2026-09-04"


class _Router:
    """The router as `build_compactor` uses it: no summary model can be built, the leaves still can."""

    def __init__(self, leaf) -> None:
        self._leaf = leaf

    def get_summarizer(self):
        raise ValueError("summary provider 'openai' could not be built (missing credentials or base URL?)")

    def get_provider(self, name: str, model_hint: str = ""):
        return self._leaf if name == "anthropic" else None

    def is_free(self, name: str) -> bool:
        return False


async def test_the_built_compactor_compacts_on_the_server_without_a_summary_model(
    settings, session_manager, cost_tracker, registry, caplog
):
    from pincer.core.compaction import build_compactor

    await _seed(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000), _COMPACTED]) as llm:
        compactor = build_compactor(settings, _Router(llm.provider), cost_tracker, session_manager, None)
        assert isinstance(compactor, Compactor)  # not None: a missing summary model no longer switches it off
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].raw == [_BLOCK]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "History compaction by summary is off: summary provider 'openai' could not be built "
        "(missing credentials or base URL?)"
    ]


async def test_an_endpoint_without_server_compaction_and_no_summary_model_keeps_its_history(
    settings, session_manager, cost_tracker, registry, caplog
):
    from pincer.core.compaction import build_compactor

    settings.server_compaction_providers = []
    await _seed(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, [_reply("Reply 1", 100_000)]) as llm:
        compactor = build_compactor(settings, _Router(llm.provider), cost_tracker, session_manager, None)
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        await agent.wait_compactions()

    assert len(llm.requests) == 1
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) == 12
    # told once at startup, then quiet: not a failed summary call per turn
    assert [r.getMessage()[:40] for r in caplog.records if r.levelno >= logging.WARNING] == [
        "History compaction by summary is off: su"
    ]


async def test_a_last_request_that_sent_nothing_is_not_compacted(settings, session_manager, cost_tracker):
    session = await session_manager.get_or_create("user1", "test")
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="one"))
    async with ScriptedAnthropic(settings, []) as llm:
        outcome = await _compactor(settings, llm, session_manager, cost_tracker).compact(session, _last_request([]))
        assert outcome is CompactionOutcome.SKIPPED

    assert llm.requests == []  # the API would answer 400 compaction_nothing_to_summarize
