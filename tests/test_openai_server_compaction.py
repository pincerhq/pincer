"""OpenAI and Azure compact the conversation on the server (issue #358, slice 9).

A turn on a Responses endpoint whose last prompt went over the limit makes `/responses/compact` return a
window (user messages and an encrypted compaction item). The window replaces the messages that request
sent, is stored unchanged, and goes back as the start of the next `/responses` input. The OpenAI SDK's
own client runs on a MockTransport (tests/openai_wire.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from anthropic_sse import ScriptedAnthropic, text
from openai_wire import (
    Compaction,
    ScriptedOpenAI,
    Turn,
    compaction_item,
    message_item,
    reasoning_item,
    usage,
    user_item,
)
from pydantic import SecretStr
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
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import ImageContent, LLMMessage, MessageRole
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.models.costs import CostLog
from pincer.tools.registry import ToolRegistry

_BIG = 40_000  # characters per earlier message
_NOTICE = "[Earlier conversation was compacted by OpenAI; its content is encrypted and cannot be read here]"
_WIRE = "openai:gpt-5.2"
# What /responses/compact returns: the user messages it retains, then the encrypted item
_WINDOW = [user_item("Which tea?", "msg_u1"), compaction_item("cmp_1", "ENC-CMP-1")]
_OVER_THE_LIMIT = usage(100_000, 50)


@pytest.fixture
def openai_settings(settings):
    """OpenAI on a reasoning model, the developer's effort/thinking env pinned."""
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


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def create_event(title: str = "") -> str:
        return "created"

    registry.register(name="create_event", description="Create a calendar event", handler=create_event)
    return registry


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


async def _stored(session_manager, user_id: str, channel: str) -> list[dict[str, Any]]:
    """The session's messages as the database holds them."""
    row = await session_manager._store.load(
        f"{channel}:{user_id}", user_id=user_id, channel=channel, legacy_fallback=False
    )
    assert row is not None
    return json.loads(row["messages"])


async def _until_requests(llm: ScriptedOpenAI, count: int) -> None:
    """Wait (bounded) until the endpoint has received `count` requests."""
    for _ in range(500):
        if len(llm.requests) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(llm.requests)} of {count} requests arrived")


async def _photo_rows(session_manager, user_id: str, channel: str) -> list[str]:
    """The shas of the photo rows the database holds for the session."""
    row = await session_manager._store.load(
        f"{channel}:{user_id}", user_id=user_id, channel=channel, legacy_fallback=False
    )
    assert row is not None
    return sorted(photo.sha256 for photo in row["images"])


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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


def _compactor(settings, llm: ScriptedOpenAI, session_manager, cost_tracker, memory=None) -> Compactor:
    server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
    return Compactor(
        None,
        None,
        cost_tracker,
        session_manager,
        memory,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        server=server,
    )


def _agent(settings, llm: ScriptedOpenAI, session_manager, cost_tracker, registry, memory=None) -> Agent:
    compactor = _compactor(settings, llm, session_manager, cost_tracker, memory)
    return Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)


async def test_a_turn_over_the_limit_posts_what_the_last_request_sent_to_responses_compact_and_keeps_the_window(
    openai_settings, session_manager, cost_tracker, registry, caplog
):
    caplog.set_level(logging.INFO, logger="pincer.core.compaction")
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(openai_settings, [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW)])
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()

    # preconditions: the turn's own request carries the whole conversation
    assert llm.paths[0] == "/v1/responses"
    turn = llm.requests[0]
    assert turn["input"][0]["content"] == _earlier(0) and len(turn["input"]) == 11
    assert turn["instructions"] and turn["tools"]

    assert llm.paths == ["/v1/responses", "/v1/responses/compact"]
    assert llm.requests[1] == {"model": "gpt-5.2", "input": turn["input"]}

    session = await session_manager.get_or_create("user1", "test")
    assert [m.role for m in session.messages] == [MessageRole.SYSTEM, MessageRole.ASSISTANT]
    assert (await _stored(session_manager, "user1", "test"))[0] == {
        "role": "system",
        "content": _NOTICE,
        "raw": _WINDOW,
        "wire": _WIRE,
    }
    assert "Session test:user1 compacted on the server: 11 messages replaced by a window of 2 items" in caplog.text


async def test_the_next_responses_request_starts_with_the_window_unchanged_also_after_a_reload(
    openai_settings, session_manager, cost_tracker, registry
):
    # an extra field on a known item and an item type the SDK does not know must survive as they came
    window = [
        user_item("Which tea?", "msg_u1"),
        {"id": "ft_1", "type": "future_item", "payload": "P-1"},
        compaction_item("cmp_1", "ENC-CMP-1", future_field="F-1"),
    ]
    reply_1 = [reasoning_item("rs_1", "ENC-1"), message_item("Reply 1", "msg_r1")]
    replies = [(reply_1, _OVER_THE_LIMIT), Compaction(window), [message_item("Reply 2", "msg_r2")]]
    replies.append([message_item("Reply 3", "msg_r3")])
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(openai_settings, replies)
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()
    assert llm.paths == ["/v1/responses", "/v1/responses/compact"]  # precondition: the compaction ran

    await agent.handle_message("user1", "test", "turn 2", memory=False)
    turn_2 = llm.requests[2]["input"]
    assert turn_2[:3] == window
    assert turn_2[3:5] == reply_1  # the kept reply goes back with its reasoning
    assert [item.get("type") or item["role"] for item in turn_2[5:]] == ["user"]
    assert turn_2[5]["content"][-1] == {"type": "input_text", "text": "turn 2"}

    before = await session_manager.get_or_create("user1", "test")
    await session_manager.evict("test:user1")
    await agent.handle_message("user1", "test", "turn 3", memory=False)
    assert await session_manager.get_or_create("user1", "test") is not before  # precondition: loaded from storage
    assert llm.requests[3]["input"][:3] == window


async def test_a_turn_that_arrives_while_the_compaction_runs_stays_after_the_window(
    openai_settings, session_manager, cost_tracker, registry
):
    gate = asyncio.Event()
    replies = [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW, gate=gate)]
    replies.append([message_item("Reply 2", "msg_r2")])
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(openai_settings, replies)
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await _until_requests(llm, 2)
    assert llm.paths[1] == "/v1/responses/compact"  # precondition: the compaction is in flight, held by the gate

    await agent.handle_message("user1", "test", "turn 2", memory=False)
    gate.set()
    await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert [m.role for m in session.messages] == [
        MessageRole.SYSTEM,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [m.content for m in session.messages[1:]] == ["Reply 1", "turn 2", "Reply 2"]
    assert [m["role"] for m in await _stored(session_manager, "user1", "test")] == [
        "system",
        "assistant",
        "user",
        "assistant",
    ]


async def test_a_history_that_changed_while_the_compaction_ran_discards_the_window(
    openai_settings, session_manager, cost_tracker, registry
):
    gate = asyncio.Event()
    replies = [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW, gate=gate)]
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(openai_settings, replies)
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await _until_requests(llm, 2)
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) == 12  # precondition: the seeded messages, the turn's user message and the reply

    assert await session_manager.cut(session, session.messages[:2])  # the head changes under the compaction
    gate.set()
    await agent.wait_compactions()

    assert not any(m.role == MessageRole.SYSTEM for m in session.messages)
    assert len(session.messages) == 10
    stored = await _stored(session_manager, "user1", "test")
    assert len(stored) == 10 and all("raw" not in m or m["role"] == "assistant" for m in stored)


async def test_the_swap_deletes_the_photos_only_the_replaced_messages_showed(
    openai_settings, session_manager, cost_tracker, registry
):
    old_photo, new_photo = b"photo-in-a-replaced-message", b"photo-sent-while-the-compaction-ran"
    gate = asyncio.Event()
    replies = [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW, gate=gate)]
    replies.append([message_item("Reply 2", "msg_r2")])
    await _seed(session_manager, "user1", "test", photo=old_photo)
    llm = ScriptedOpenAI(openai_settings, replies)
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await _until_requests(llm, 2)
    await agent.handle_message("user1", "test", "turn 2", images=[(new_photo, "image/jpeg")], memory=False)
    assert await _photo_rows(session_manager, "user1", "test") == sorted([_sha(old_photo), _sha(new_photo)])

    gate.set()
    await agent.wait_compactions()

    assert await _photo_rows(session_manager, "user1", "test") == [_sha(new_photo)]
    kept = [m for m in await _stored(session_manager, "user1", "test") if m.get("images")]
    assert [m["images"] for m in kept] == [[{"sha256": _sha(new_photo), "media_type": "image/jpeg"}]]


@pytest.mark.parametrize("memory", [True, False])
async def test_the_encrypted_window_is_never_saved_as_a_memory(
    openai_settings, session_manager, cost_tracker, registry, memory
):
    await _seed(session_manager, "user1", "test")
    memories = _Memories()
    llm = ScriptedOpenAI(openai_settings, [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW)])
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry, memories)
    await agent.handle_message("user1", "test", "turn 1", memory=memory)
    await agent.wait_compactions()

    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].raw == _WINDOW  # compacted either way
    assert memories.stored == []


@pytest.mark.parametrize(
    ("billed", "expected"),
    [
        (usage(42_897, 12_000), (42_897, 0, 0, 12_000)),
        (usage(1_000, 200, cached=600, written=100, reasoning=150), (300, 600, 100, 200)),
    ],
    ids=["plain", "cached and written"],
)
async def test_the_compaction_call_is_recorded_with_its_usage_and_the_session_id(
    openai_settings, session_manager, cost_tracker, registry, billed, expected
):
    """`test-fixture-model` is priced in tests/fixtures/pricing."""
    settings = openai_settings.model_copy(update={"openai_model": "test-fixture-model"})
    await _seed(session_manager, "user1", "test")
    replies = [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT), Compaction(_WINDOW, billed)]
    llm = ScriptedOpenAI(settings, replies, model="test-fixture-model")
    agent = _agent(settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()

    turn, compaction_call = await _cost_rows(cost_tracker)
    assert (turn["uncached"], turn["read"], turn["output"]) == (100_000, 0, 50)  # precondition: the turn is recorded
    uncached, read, written, output = expected
    assert compaction_call == {
        "session_id": "test:user1",
        "provider": "openai",
        "model": "test-fixture-model",
        "uncached": uncached,
        "read": read,
        "write_5m": written,
        "write_1h": 0,
        "output": output,
    }


async def test_with_the_budget_reached_no_compaction_call_is_made_and_nothing_changes(
    openai_settings, session_manager, tmp_path, registry, caplog
):
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    settings = openai_settings.model_copy(update={"openai_model": "test-fixture-model"})
    # A budget the turn's own call crosses: it may run (nothing spent yet), and it is recorded
    costs = CostTracker(tmp_path / "budget.db", daily_budget=1e-9, prices=load_price_table())
    await costs.initialize()
    await _seed(session_manager, "user1", "test", photo=b"photo-in-the-oldest-message")
    try:
        llm = ScriptedOpenAI(
            settings, [([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT)], model="test-fixture-model"
        )
        agent = _agent(settings, llm, session_manager, costs, registry)
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        assert await costs.budget_reached()  # precondition: the turn's call reached the budget
        before = await _stored(session_manager, "user1", "test")
        await agent.wait_compactions()
    finally:
        await costs.close()

    assert llm.paths == ["/v1/responses"]  # no compaction request
    assert await _stored(session_manager, "user1", "test") == before
    assert len(await _photo_rows(session_manager, "user1", "test")) == 1
    assert len(before) == 12
    assert "Daily budget reached: session test:user1 is not compacted" in caplog.text


_NO_WINDOW_LOG = "Session test:user1: the server returned no compaction item, history kept; compaction will be retried"


@pytest.mark.parametrize(
    ("answer", "warning", "billed"),
    [
        (Compaction([user_item("Which tea?")], usage(500, 20)), _NO_WINDOW_LOG, True),
        (Compaction([], usage(500, 20)), _NO_WINDOW_LOG, True),
        (Compaction([user_item("Which tea?"), compaction_item(encrypted="")], usage(500, 20)), _NO_WINDOW_LOG, True),
        (400, "Compacting session test:user1 on the server failed", False),
        (500, "Compacting session test:user1 on the server failed", False),
        (httpx.ConnectError("refused"), "Compacting session test:user1 on the server failed", False),
    ],
    ids=["no item", "empty window", "empty encrypted content", "HTTP 400", "HTTP 500", "connection error"],
)
async def test_without_a_window_nothing_changes_and_a_later_turn_retries(
    openai_settings, session_manager, cost_tracker, registry, caplog, answer, warning, billed
):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    settings = openai_settings.model_copy(update={"openai_model": "test-fixture-model"})
    await _seed(session_manager, "user1", "test", photo=b"photo-in-the-oldest-message")
    memories = _Memories()
    replies = [
        ([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT),
        answer,
        ([message_item("Reply 2", "msg_r2")], _OVER_THE_LIMIT),
        Compaction(_WINDOW),
    ]
    llm = ScriptedOpenAI(settings, replies, model="test-fixture-model")
    agent = _agent(settings, llm, session_manager, cost_tracker, registry, memories)
    await agent.handle_message("user1", "test", "turn 1", memory=True)
    before = await _stored(session_manager, "user1", "test")
    await agent.wait_compactions()

    assert llm.paths == ["/v1/responses", "/v1/responses/compact"]  # precondition: the compaction was asked
    assert await _stored(session_manager, "user1", "test") == before and len(before) == 12
    assert len(await _photo_rows(session_manager, "user1", "test")) == 1
    assert warning in caplog.text
    assert memories.stored == []
    assert len(await _cost_rows(cost_tracker)) == (2 if billed else 1)  # a 200 is billed, an error is not

    await agent.handle_message("user1", "test", "turn 2", memory=True)  # still over the limit: tried again
    await agent.wait_compactions()
    assert llm.paths[2:] == ["/v1/responses", "/v1/responses/compact"]
    session = await session_manager.get_or_create("user1", "test")
    assert [m.raw for m in session.messages][0] == _WINDOW and len(session.messages) == 2


@pytest.mark.parametrize(
    ("update", "provider", "leaf_kind", "expected"),
    [
        ({}, "openai", "openai", CompactionStrategy.OPENAI_SERVER),
        ({}, "azure", "azure", CompactionStrategy.OPENAI_SERVER),
        ({"responses_api_providers": []}, "openai", "openai", CompactionStrategy.CLIENT_SUMMARY),
        ({"responses_api_providers": ["azure"]}, "openai", "openai", CompactionStrategy.CLIENT_SUMMARY),
        ({"responses_api_providers": ["gw"]}, "gw", "gw", CompactionStrategy.OPENAI_SERVER),
        ({}, "gw", "gw", CompactionStrategy.CLIENT_SUMMARY),
        ({"server_compaction_providers": []}, "openai", "openai", CompactionStrategy.OPENAI_SERVER),
        (
            {"server_compaction_providers": ["openai"], "responses_api_providers": []},
            "openai",
            "openai",
            CompactionStrategy.CLIENT_SUMMARY,
        ),
        ({}, "anthropic", "anthropic", CompactionStrategy.ANTHROPIC_SERVER),
        ({"responses_api_providers": ["anthropic"]}, "anthropic", "anthropic", CompactionStrategy.ANTHROPIC_SERVER),
        ({}, "openai", None, CompactionStrategy.CLIENT_SUMMARY),
    ],
    ids=[
        "openai",
        "azure",
        "removed from the list",
        "another name listed",
        "a compatible name added to the list",
        "a compatible name not listed",
        "empty server_compaction_providers leaves OpenAI alone",
        "an OpenAI name in server_compaction_providers enables nothing",
        "Anthropic",
        "an Anthropic leaf named in the Responses list",
        "no leaf",
    ],
)
def test_the_strategy_is_the_responses_list_for_an_openai_wire_endpoint(
    openai_settings, update, provider, leaf_kind, expected
):
    settings = openai_settings.model_copy(
        update={
            "openai_compatible_provider": "azure",
            "openai_compatible_base_url": "https://res.openai.azure.com/openai/v1/",
            "openai_compatible_model": "gpt-6.1-terra",
            **update,
        }
    )
    leaves = {
        "openai": lambda: OpenAICompatibleProvider(settings, "openai"),
        "azure": lambda: OpenAICompatibleProvider(settings, "azure"),
        "gw": lambda: OpenAICompatibleProvider(settings, "azure"),
        "anthropic": lambda: AnthropicCompatibleProvider(settings, "anthropic"),
        None: lambda: None,
    }
    assert pick_strategy(settings, provider, leaves[leaf_kind]()) is expected


async def test_an_endpoint_off_the_responses_list_keeps_the_summary_model_exactly(
    openai_settings, session_manager, cost_tracker, registry
):
    settings = openai_settings.model_copy(update={"responses_api_providers": []})
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(settings, ["Reply 1", "A summary of the earlier talk"], prompt_tokens=100_000)
    server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
    compactor = Compactor(
        llm.provider,
        "gpt-5.2",
        cost_tracker,
        session_manager,
        None,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        server=server,
    )
    agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry, compactor=compactor)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()

    assert llm.paths == ["/v1/chat/completions"] * 2  # the turn and the summary: nothing on /responses
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].content == "[Previous conversation summary]\nA summary of the earlier talk"
    assert session.messages[0].raw == []


_CIPHERTEXT = ["ENC-CMP-1", "ENC-1", "cmp_1", "future_item"]


async def _compacted_conversation(settings, session_manager, cost_tracker, registry) -> None:
    """Turn 1 on `openai:gpt-5.2` (its reply holds reasoning) and its compaction: the session is [window, reply]."""
    await _seed(session_manager, "user1", "test")
    reply = [reasoning_item("rs_1", "ENC-1"), message_item("Reply 1", "msg_r1")]
    window = [*_WINDOW[:1], {"id": "ft_1", "type": "future_item", "payload": "P"}, *_WINDOW[1:]]
    llm = ScriptedOpenAI(settings, [(reply, _OVER_THE_LIMIT), Compaction(window)])
    agent = _agent(settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()
    session = await session_manager.get_or_create("user1", "test")
    assert session.messages[0].raw == window and session.messages[0].wire == _WIRE  # precondition: a stored window


def _assert_no_ciphertext(body: dict[str, Any]) -> None:
    sent = json.dumps(body)
    assert [marker for marker in _CIPHERTEXT if marker in sent] == []


async def test_another_model_on_the_same_endpoint_sees_the_notice_not_the_window(
    openai_settings, session_manager, cost_tracker, registry
):
    await _compacted_conversation(openai_settings, session_manager, cost_tracker, registry)
    settings = openai_settings.model_copy(update={"openai_model": "gpt-5.2-mini"})
    llm = ScriptedOpenAI(settings, [[message_item("Reply 2", "msg_r2")]], model="gpt-5.2-mini")
    await _agent(settings, llm, session_manager, cost_tracker, registry).handle_message(
        "user1", "test", "turn 2", memory=False
    )

    body = llm.requests[0]
    assert body["model"] == "gpt-5.2-mini"
    assert body["input"][:2] == [
        {"role": "system", "content": _NOTICE},
        {"role": "assistant", "content": "Reply 1"},  # the kept reply is rebuilt from its text
    ]
    _assert_no_ciphertext(body)


async def test_a_chat_completions_endpoint_sees_the_notice_not_the_window(
    openai_settings, session_manager, cost_tracker, registry
):
    await _compacted_conversation(openai_settings, session_manager, cost_tracker, registry)
    settings = openai_settings.model_copy(update={"responses_api_providers": []})
    llm = ScriptedOpenAI(settings, ["Reply 2"])
    await _agent(settings, llm, session_manager, cost_tracker, registry).handle_message(
        "user1", "test", "turn 2", memory=False
    )

    assert llm.paths == ["/v1/chat/completions"]
    assert {"role": "system", "content": _NOTICE} in llm.requests[0]["messages"]
    _assert_no_ciphertext(llm.requests[0])


async def test_an_anthropic_endpoint_sees_the_notice_not_the_window(
    openai_settings, session_manager, cost_tracker, registry
):
    await _compacted_conversation(openai_settings, session_manager, cost_tracker, registry)
    settings = openai_settings.model_copy(update={"default_provider": "anthropic", "default_model": "claude-haiku-5-5"})
    async with ScriptedAnthropic(settings, [([text("Reply 2")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "turn 2", memory=False)

    messages = llm.requests[0]["messages"]
    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": _NOTICE}]}
    assert messages[1] == {"role": "assistant", "content": "Reply 1"}
    _assert_no_ciphertext(llm.requests[0])


async def test_a_second_compaction_takes_the_first_window_as_input_and_replaces_it(
    openai_settings, session_manager, cost_tracker, registry
):
    second = [user_item("Which tea? And coffee?", "msg_u2"), compaction_item("cmp_2", "ENC-CMP-2")]
    replies = [
        ([message_item("Reply 1", "msg_r1")], _OVER_THE_LIMIT),
        Compaction(_WINDOW),
        ([message_item("Reply 2", "msg_r2")], _OVER_THE_LIMIT),
        Compaction(second),
    ]
    await _seed(session_manager, "user1", "test")
    llm = ScriptedOpenAI(openai_settings, replies)
    agent = _agent(openai_settings, llm, session_manager, cost_tracker, registry)
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.wait_compactions()
    await agent.handle_message("user1", "test", "turn 2", memory=False)
    await agent.wait_compactions()

    assert llm.paths[1::2] == ["/v1/responses/compact"] * 2  # precondition: two compactions
    assert llm.requests[3]["input"][:2] == _WINDOW  # the first window leads what the second one compacts
    assert llm.requests[3] == {"model": "gpt-5.2", "input": llm.requests[2]["input"]}
    session = await session_manager.get_or_create("user1", "test")
    assert [m.role for m in session.messages] == [MessageRole.SYSTEM, MessageRole.ASSISTANT]
    stored = await _stored(session_manager, "user1", "test")
    assert stored[0]["raw"] == second and "ENC-CMP-1" not in json.dumps(stored)


# ── compact() outcomes and the emergency cut on this path ────────────────────

_NO_ITEM = Compaction([user_item("Which tea?")], usage(500, 20))
_EXITS = [
    # (id, outcome, scenario)
    ("a window replaced the oldest", CompactionOutcome.COMPACTED, {"script": [Compaction(_WINDOW)]}),
    (
        "a window above the hard limit: no cut",
        CompactionOutcome.COMPACTED,
        {"script": [Compaction(_WINDOW)], "prompt": 170_000},
    ),
    ("not due", CompactionOutcome.SKIPPED, {"script": [], "prompt": 70_000}),
    ("nothing was sent", CompactionOutcome.SKIPPED, {"script": [], "nothing_sent": True}),
    ("swap discarded", CompactionOutcome.SKIPPED, {"script": [Compaction(_WINDOW)], "edit": True}),
    ("budget reached", CompactionOutcome.FAILED, {"script": [], "budget": True}),
    ("no compaction item", CompactionOutcome.FAILED, {"script": [_NO_ITEM]}),
    ("call raised", CompactionOutcome.FAILED, {"script": [400]}),
    ("budget reached above the hard limit", CompactionOutcome.CUT, {"script": [], "budget": True, "prompt": 170_000}),
    ("no compaction item above the hard limit", CompactionOutcome.CUT, {"script": [_NO_ITEM], "prompt": 170_000}),
    ("call raised above the hard limit", CompactionOutcome.CUT, {"script": [400], "prompt": 170_000}),
]


async def _history(sessions, window: bool = False):
    """A session of 10 big earlier messages (user first, alternating), "turn 1" and "Reply 1"; with `window`
    it starts with a compacted window. Returns it with the messages the turn's last request sent."""
    session = await sessions.get_or_create("user1", "test")
    if window:
        await sessions.add_message(
            session, LLMMessage(role=MessageRole.SYSTEM, content=_NOTICE, raw=list(_WINDOW), wire=_WIRE)
        )
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await sessions.add_message(session, LLMMessage(role=role, content=_earlier(index)))
    await sessions.add_message(session, LLMMessage(role=MessageRole.USER, content="turn 1"))
    sent = list(session.messages)
    await sessions.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="Reply 1"))
    return session, sent


async def _spent(tmp_path):
    """A cost tracker whose daily budget is already spent."""
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    costs = CostTracker(tmp_path / "spent.db", daily_budget=1e-9, prices=load_price_table())
    await costs.initialize()
    await costs.record("openai", "test-fixture-model", usage_of(1_000))
    assert await costs.budget_reached()  # precondition
    return costs


def usage_of(input_tokens: int):
    from pincer.llm.base import LLMResponse

    return LLMResponse(input_tokens=input_tokens).usage


@pytest.mark.parametrize(("outcome", "case"), [e[1:] for e in _EXITS], ids=[e[0] for e in _EXITS])
async def test_compact_reports_the_outcome_of_every_exit_on_a_responses_endpoint(
    openai_settings, session_manager, cost_tracker, tmp_path, outcome, case
):
    settings = openai_settings.model_copy(update={"openai_model": "test-fixture-model"})
    session, sent = await _history(session_manager)
    before = list(session.messages)
    costs = await _spent(tmp_path) if case.get("budget") else cost_tracker
    gate = asyncio.Event()
    script = [Compaction(_WINDOW, gate=gate)] if case.get("edit") else list(case["script"])
    llm = ScriptedOpenAI(settings, script, model="test-fixture-model")
    request = SentRequest("openai", "test-fixture-model", None, None)
    last = LastRequest.measure(
        case.get("prompt", 100_000), [] if case.get("nothing_sent") else sent, 2_000, request=request
    )
    server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
    compactor = Compactor(
        None,
        None,
        costs,
        session_manager,
        None,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        hard_tokens=160_000,
        server=server,
    )
    try:
        if case.get("edit"):
            task = asyncio.create_task(compactor.compact(session, last))
            await _until_requests(llm, 1)
            # as a tool-pair repair would: the oldest message is a new object now
            session.messages = [LLMMessage(role=MessageRole.USER, content="edited"), *session.messages[1:]]
            before = list(session.messages)
            gate.set()
            result = await asyncio.wait_for(task, timeout=5)
        else:
            result = await compactor.compact(session, last)
    finally:
        if costs is not cost_tracker:
            await costs.close()

    assert result is outcome
    if outcome is CompactionOutcome.COMPACTED:
        assert session.messages[0].raw == _WINDOW
    elif outcome is CompactionOutcome.CUT:
        assert len(session.messages) < len(before) and session.messages[0].role == MessageRole.USER
    else:
        assert session.messages == before and all(a is b for a, b in zip(session.messages, before, strict=True))


async def test_the_emergency_cut_keeps_a_stored_window_and_drops_the_turns_after_it(
    openai_settings, session_manager, cost_tracker, caplog
):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    session, sent = await _history(session_manager, window=True)
    assert session.messages[0].raw == _WINDOW  # precondition: the window leads the history
    llm = ScriptedOpenAI(openai_settings, [400])
    request = SentRequest("openai", "gpt-5.2", None, None)
    last = LastRequest.measure(170_000, sent, 2_000, request=request)
    server = ServerCompaction(openai_settings, lambda _name, _model: llm.provider, lambda _name: False)
    compactor = Compactor(
        None,
        None,
        cost_tracker,
        session_manager,
        None,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        hard_tokens=160_000,
        server=server,
    )

    assert await compactor.compact(session, last) is CompactionOutcome.CUT

    assert llm.paths == ["/v1/responses/compact"]  # the compaction was tried first
    assert [m.role for m in session.messages if m.role == MessageRole.SYSTEM] == [MessageRole.SYSTEM]
    assert session.messages[0].raw == _WINDOW  # the window is the memory of everything before: it stays
    assert "ENC-CMP-1" in json.dumps(await _stored(session_manager, "user1", "test"))
    assert "prompt of 170000 tokens is over the hard limit of 160000 and compaction failed; dropped the oldest 8" in (
        caplog.text
    )


@pytest.mark.parametrize("path", ["stream", "voice"])
async def test_streamed_and_voice_turns_compact_what_their_last_request_sent(
    openai_settings, session_manager, cost_tracker, registry, path
):
    settings = openai_settings.model_copy(update={"voice_turn_model": "", "voice_max_response_tokens": 220})
    await session_manager.onboarding.complete("user1", path, now=1.0)
    await _seed(session_manager, "user1", path)
    llm = ScriptedOpenAI(settings, [Turn(text=["Reply 1"], prompt_tokens=100_000), Compaction(_WINDOW)])
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

    assert llm.paths == ["/v1/responses", "/v1/responses/compact"]
    turn, request = llm.requests
    assert turn["stream"] is True and len(turn["input"]) == 11  # precondition: the streamed turn sent everything
    if path == "voice":
        assert turn["max_output_tokens"] == 220  # precondition: the voice request
    assert request == {"model": "gpt-5.2", "input": turn["input"]}
    session = await session_manager.get_or_create("user1", path)
    assert session.messages[0].raw == _WINDOW
