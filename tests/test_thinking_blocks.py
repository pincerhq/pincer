"""Thinking blocks ride along within one agent turn and are gone by the next (issue #345)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from anthropic_sse import ScriptedAnthropic, redacted, text, thinking, tool_use

from pincer.core.agent import Agent, StreamEventType, _sanitize_tool_pairs
from pincer.core.session import SessionManager
from pincer.exceptions import BudgetExceededError, LLMError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    StreamTurnEvent,
    ThinkingBlock,
    ToolCall,
)
from pincer.llm.router import LLMRouter
from pincer.tools.registry import ToolRegistry
from pincer.tools.selection import ToolSelector

# Sentinels: none of these may ever leave the model round-trip
_THOUGHT = "SECRET-THOUGHT-7f3a"
_SIGNATURE = "SIG-7f3a-signature"
_REDACTED = "REDACTED-7f3a-data"
_SENTINELS = (_THOUGHT, _SIGNATURE, _REDACTED)


class _Tools:
    """A registry with one tool that records its runs."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.registry = ToolRegistry()

        async def create_event(title: str = "") -> str:
            self.runs.append(title)
            return "created"

        self.registry.register(name="create_event", description="Create a calendar event", handler=create_event)


@pytest.fixture
def tools() -> _Tools:
    return _Tools()


def _block_types(request: dict[str, Any]) -> list[str]:
    return [
        block["type"]
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ]


def _assert_no_sentinel(where: str, value: str) -> None:
    for sentinel in _SENTINELS:
        assert sentinel not in value, f"thinking leaked into {where}"


def _assert_no_sentinel_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    pincer_records = [r for r in caplog.records if r.name == "pincer" or r.name.startswith("pincer.")]
    assert pincer_records, "precondition: the pincer loggers were captured"
    for record in pincer_records:
        _assert_no_sentinel(f"log {record.name}", record.getMessage())


async def test_thinking_rides_along_within_a_turn_and_is_gone_by_the_next(
    settings, session_manager, cost_tracker, tools, caplog
):
    script = [
        (
            [
                thinking(_THOUGHT, _SIGNATURE),
                redacted(_REDACTED),
                tool_use("toolu_1", "create_event", {"title": "Dentist"}),
            ],
            "tool_use",
        ),
        ([text("Booked.")], "end_turn"),
        ([text("You're welcome.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            first = await agent.handle_message("user1", "test", "Book the dentist", memory=False)
            session = await session_manager.get_or_create("user1", "test")
            second = await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert tools.runs == ["Dentist"]
    assert len(llm.requests) == 3
    assistant = [m for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"][:2] == [
        {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
        {"type": "redacted_thinking", "data": _REDACTED},
    ]
    # Turn 2 runs on the same cached session object, and sends no thinking
    assert await session_manager.get_or_create("user1", "test") is session
    assert "thinking" not in _block_types(llm.requests[2])
    assert "redacted_thinking" not in _block_types(llm.requests[2])
    _assert_no_sentinel("turn 2's request", llm.raw_requests[2])
    assert all(not m.thinking_blocks for m in session.messages)
    # Never visible: channel text, saved session, logs
    _assert_no_sentinel("channel text", first.text + second.text)
    stored = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert stored is not None
    _assert_no_sentinel("saved session", json.dumps(stored["messages"]))
    _assert_no_sentinel_in_logs(caplog)


_THINKING = [
    ThinkingBlock("thinking", thinking=_THOUGHT, signature=_SIGNATURE),
    ThinkingBlock("redacted_thinking", data=_REDACTED),
]
_DONE = LLMResponse(content="done", model="test-model", input_tokens=10, output_tokens=5, stop_reason="end_turn")


def _thinking_tool_response(tool_id: str = "tc1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=tool_id, name="create_event", arguments={"title": "Dentist"})],
        model="test-model",
        input_tokens=50,
        output_tokens=40,
        stop_reason="tool_use",
        thinking_blocks=list(_THINKING),
    )


def _sent_thinking(messages: list[LLMMessage]) -> list[ThinkingBlock]:
    """The thinking blocks a request carried, read when it was made."""
    return [block for m in messages for block in m.thinking_blocks]


async def test_streaming_chat_final_stream_gets_no_thinking(
    settings, mock_llm, session_manager, cost_tracker, tools, caplog
):
    """The final stream() sends no tools, a change under the turn's thinking (Q2c): the
    turn's complete() requests carry the blocks, the final stream() request carries none."""
    responses = [_thinking_tool_response(), _DONE]
    completed_with: list[list[ThinkingBlock]] = []
    streamed_with: list[list[ThinkingBlock]] = []

    async def complete(messages, **kwargs):
        completed_with.append(_sent_thinking(messages))
        return responses.pop(0)

    async def stream(messages, **kwargs):
        streamed_with.append(_sent_thinking(messages))
        yield "Booked."

    mock_llm.complete.side_effect = complete
    mock_llm.stream = stream
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    with caplog.at_level(logging.DEBUG, logger="pincer"):
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]

    assert tools.runs == ["Dentist"]
    assert completed_with == [[], _THINKING]
    assert streamed_with == [[]]
    session = await session_manager.get_or_create("user1", "web")
    assert _sent_thinking(session.messages) == []
    for chunk in chunks:
        _assert_no_sentinel(f"{chunk.type} chunk", chunk.content)
    _assert_no_sentinel_in_logs(caplog)


class _ScriptedVoice(BaseLLMProvider):
    """stream_turn replays scripted turns, one per request, recording the thinking each request carried."""

    def __init__(self, turns: list[list[StreamTurnEvent]]) -> None:
        self.turns = list(turns)
        self.sent: list[list[ThinkingBlock]] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use stream()")
        yield ""  # pragma: no cover

    async def stream_turn(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=None,
        temperature=None,
        system=None,
        reasoning_effort=None,
        thinking=None,
    ):
        self.sent.append(_sent_thinking(messages))
        for event in self.turns.pop(0):
            yield event

    async def close(self) -> None:
        pass


def _voice_agent(settings, provider, session_manager, cost_tracker, tools) -> Agent:
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    return Agent(settings, provider, session_manager, cost_tracker, tools.registry)


async def test_voice_follow_up_request_gets_the_turns_thinking(settings, session_manager, cost_tracker, tools, caplog):
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(text="Einen Moment. "), StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    with caplog.at_level(logging.DEBUG, logger="pincer"):
        chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]

    assert tools.runs == ["Dentist"]
    assert provider.sent == [[], _THINKING]
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert _sent_thinking(session.messages) == []
    # The phone channel speaks and transcribes only these chunks
    for chunk in chunks:
        _assert_no_sentinel(f"{chunk.type} chunk", chunk.content)
    _assert_no_sentinel_in_logs(caplog)


async def test_voice_cut_off_retry_never_sends_the_discarded_thinking(settings, session_manager, cost_tracker, tools):
    discarded = [ThinkingBlock("thinking", thinking="DISCARDED-THOUGHT", signature="DISCARDED-SIG")]
    cut_off = LLMResponse(
        content="",
        model="test-model",
        input_tokens=50,
        output_tokens=220,
        stop_reason="max_tokens",
        thinking_blocks=discarded,
    )
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(response=cut_off)],  # nothing streamed: retried silently
            [StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]

    assert chunks[-1].content == "Erledigt."
    assert provider.sent == [[], [], _THINKING]


async def test_chat_turn_strips_thinking_when_it_fails(settings, mock_llm, session_manager, cost_tracker, tools):
    mock_llm.complete.side_effect = [_thinking_tool_response(), LLMError("Anthropic API error: 500 overloaded"), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    session = await session_manager.get_or_create("user1", "test")
    assert any(m.tool_calls for m in session.messages)  # precondition: the thinking message was saved
    assert _sent_thinking(session.messages) == []


async def test_streaming_chat_strips_thinking_on_a_budget_stop(
    settings, mock_llm, session_manager, cost_tracker, tools
):
    mock_llm.complete.side_effect = [_thinking_tool_response(), BudgetExceededError(100.0, 100.0), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tools.registry)

    chunks = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]

    assert chunks[-1].content == "Daily budget limit reached."  # precondition: the budget stop's early return
    session = await session_manager.get_or_create("user1", "web")
    assert any(m.tool_calls for m in session.messages)
    assert _sent_thinking(session.messages) == []


async def test_voice_turn_strips_thinking_when_the_caller_hangs_up_mid_turn(
    settings, session_manager, cost_tracker, tools
):
    provider = _ScriptedVoice(
        [
            [StreamTurnEvent(response=_thinking_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = _voice_agent(settings, provider, session_manager, cost_tracker, tools)

    stream = agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")
    async for chunk in stream:
        if chunk.type is StreamEventType.TOOL_START:
            break  # the caller hung up: the turn is closed mid-tool-loop
    await stream.aclose()

    assert len(provider.sent) == 1  # precondition: closed before the follow-up request
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert any(m.tool_calls for m in session.messages)
    assert _sent_thinking(session.messages) == []


async def test_saved_session_is_unchanged_by_thinking(session_manager, tmp_path):
    def history(thinking_blocks: list[ThinkingBlock]) -> list[LLMMessage]:
        call = ToolCall(id="tc1", name="create_event", arguments={"title": "Dentist"})
        return [
            LLMMessage(role=MessageRole.USER, content="Book the dentist"),
            LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call], thinking_blocks=thinking_blocks),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="tc1"),
        ]

    for user_id, blocks in (("with", list(_THINKING)), ("without", [])):
        session = await session_manager.get_or_create(user_id, "test")
        for message in history(blocks):
            await session_manager.add_message(session, message)
    assert _sent_thinking((await session_manager.get_or_create("with", "test")).messages) == _THINKING  # precondition

    rows = {
        user_id: await session_manager._store.load(
            f"test:{user_id}", user_id=user_id, channel="test", legacy_fallback=False
        )
        for user_id in ("with", "without")
    }
    assert rows["with"] is not None and rows["without"] is not None
    assert rows["with"]["messages"] == rows["without"]["messages"]
    _assert_no_sentinel("saved session", rows["with"]["messages"])

    reloaded = SessionManager(tmp_path / "test.db")
    await reloaded.initialize()
    try:
        session = await reloaded.get_or_create("with", "test")
        assert len(session.messages) == 3
        assert _sent_thinking(session.messages) == []
    finally:
        await reloaded.close()


def test_repaired_tool_call_message_drops_its_stale_thinking():
    answered = ToolCall(id="tc1", name="create_event", arguments={"title": "Dentist"})
    orphaned = ToolCall(id="tc2", name="create_event", arguments={"title": "Doctor"})
    messages = [
        LLMMessage(role=MessageRole.USER, content="Book both"),
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="Booking.",
            tool_calls=[answered, orphaned],
            thinking_blocks=list(_THINKING),
        ),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="created", tool_call_id="tc1"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[orphaned], thinking_blocks=list(_THINKING)),
        LLMMessage(role=MessageRole.USER, content="And?"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [m.tool_calls for m in clean if m.role is MessageRole.ASSISTANT] == [[answered]]  # precondition: repaired
    assert _sent_thinking(clean) == []


# ── Stable history within a turn (slice 4, Q2c) ──────

_CAP = 20  # the session_manager fixture's max_messages


async def _fill_to_cap(session_manager, user_id: str, channel: str, session_key: str | None = None) -> None:
    """Earlier turns, outside any turn, up to the session cap."""
    session = await session_manager.get_or_create(user_id, channel, session_key=session_key)
    for index in range(_CAP):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(session, LLMMessage(role=role, content=f"earlier message {index}"))
    assert len(session.messages) == _CAP  # precondition: at the cap


def _assert_extends(earlier: dict[str, Any], later: dict[str, Any]) -> None:
    """The later request of a turn resends the earlier one's history, system and tools unchanged."""
    assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
    assert later.get("system") == earlier.get("system")
    assert later.get("tools") == earlier.get("tools")


async def _stored_length(session_manager, key: str, user_id: str, channel: str) -> int:
    row = await session_manager._store.load(key, user_id=user_id, channel=channel, legacy_fallback=False)
    assert row is not None
    return len(json.loads(row["messages"]))


_TOOL_THEN_TEXT = [
    ([thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_1", "create_event", {"title": "Dentist"})], "tool_use"),
    ([text("Erledigt.")], "end_turn"),
]


async def test_voice_turn_at_the_cap_sends_an_unchanged_prefix(settings, session_manager, cost_tracker, tools):
    await _fill_to_cap(session_manager, "+4930111222", "voice")
    async with ScriptedAnthropic(settings, _TOOL_THEN_TEXT) as llm:
        agent = _voice_agent(settings, llm.provider, session_manager, cost_tracker, tools)
        chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]

    assert chunks[-1].content == "Erledigt."
    assert len(llm.requests) == 2
    _assert_extends(llm.requests[0], llm.requests[1])
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert len(session.messages) <= _CAP
    assert await _stored_length(session_manager, "voice:+4930111222", "+4930111222", "voice") <= _CAP


async def test_chat_turn_at_the_cap_sends_an_unchanged_prefix(settings, session_manager, cost_tracker, tools):
    await _fill_to_cap(session_manager, "user1", "test")
    async with ScriptedAnthropic(settings, _TOOL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        result = await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    assert result.text == "Erledigt."
    assert len(llm.requests) == 2
    _assert_extends(llm.requests[0], llm.requests[1])
    session = await session_manager.get_or_create("user1", "test")
    assert len(session.messages) <= _CAP
    assert await _stored_length(session_manager, "test:user1", "user1", "test") <= _CAP


async def test_streaming_chat_turn_at_the_cap_sends_an_unchanged_prefix(settings, session_manager, cost_tracker, tools):
    await _fill_to_cap(session_manager, "user1", "web")
    script = [*_TOOL_THEN_TEXT, ([text("Erledigt.")], "end_turn")]  # the last one is the final stream()
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]

    assert chunks[-1].content == "Erledigt."
    assert len(llm.requests) == 3
    _assert_extends(llm.requests[0], llm.requests[1])
    # The final stream() resends the history, minus the thinking (it sends no tools)
    assert llm.requests[2]["messages"] == _without_thinking(llm.requests[1]["messages"])
    session = await session_manager.get_or_create("user1", "web")
    assert len(session.messages) <= _CAP
    assert await _stored_length(session_manager, "web:user1", "user1", "web") <= _CAP


_IMAGE = (b"\x89PNG fake image bytes", "image/png")


def _image_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "image"
    ]


async def test_chat_turn_keeps_its_images_on_every_request(settings, session_manager, cost_tracker, tools):
    async with ScriptedAnthropic(settings, _TOOL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        await agent.handle_message("user1", "test", "Book what's on this card", images=[_IMAGE], memory=False)

    assert len(llm.requests) == 2
    assert len(_image_blocks(llm.requests[0])) == 1  # precondition: the image was sent
    _assert_extends(llm.requests[0], llm.requests[1])
    session = await session_manager.get_or_create("user1", "test")
    assert all(not m.images for m in session.messages)


async def test_streaming_chat_turn_keeps_its_images_on_every_request(settings, session_manager, cost_tracker, tools):
    script = [*_TOOL_THEN_TEXT, ([text("Erledigt.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        stream = agent.handle_message_stream("user1", "web", "Book what's on this card", images=[_IMAGE], memory=False)
        _chunks = [c async for c in stream]

    assert len(llm.requests) == 3
    assert len(_image_blocks(llm.requests[0])) == 1  # precondition: the image was sent
    _assert_extends(llm.requests[0], llm.requests[1])
    assert llm.requests[2]["messages"] == _without_thinking(llm.requests[1]["messages"])
    session = await session_manager.get_or_create("user1", "web")
    assert all(not m.images for m in session.messages)


_TOOL_THEN_TEXT_NO_THINKING = [
    ([tool_use("toolu_1", "create_event", {"title": "Dentist"})], "tool_use"),
    ([text("Erledigt.")], "end_turn"),
]


async def test_chat_turn_without_thinking_drops_its_image_after_the_first_request(
    settings, session_manager, cost_tracker, tools
):
    async with ScriptedAnthropic(settings, _TOOL_THEN_TEXT_NO_THINKING) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        await agent.handle_message("user1", "test", "Book what's on this card", images=[_IMAGE], memory=False)

    assert len(llm.requests) == 2
    assert len(_image_blocks(llm.requests[0])) == 1  # precondition: the image was sent
    assert _image_blocks(llm.requests[1]) == []  # no thinking to keep stable: it is not billed again


async def test_streaming_chat_turn_without_thinking_drops_its_image_after_the_first_request(
    settings, session_manager, cost_tracker, tools
):
    script = [*_TOOL_THEN_TEXT_NO_THINKING, ([text("Erledigt.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        _chunks = [
            c
            async for c in agent.handle_message_stream(
                "user1", "web", "Book what's on this card", images=[_IMAGE], memory=False
            )
        ]

    assert len(llm.requests) == 3
    assert len(_image_blocks(llm.requests[0])) == 1  # precondition: the image was sent
    assert _image_blocks(llm.requests[1]) == []
    assert _image_blocks(llm.requests[2]) == []


async def test_a_turn_whose_first_reply_has_no_thinking_drops_its_image_even_if_later_ones_think(
    settings, session_manager, cost_tracker, tools
):
    script = [
        ([tool_use("toolu_1", "create_event", {"title": "Dentist"})], "tool_use"),
        ([thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_2", "create_event", {"title": "Vet"})], "tool_use"),
        ([text("Erledigt.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        await agent.handle_message("user1", "test", "Book what's on this card", images=[_IMAGE], memory=False)

    assert len(llm.requests) == 3
    assert len(_image_blocks(llm.requests[0])) == 1  # precondition: the image was sent
    assert _image_blocks(llm.requests[1]) == []  # dropped before any thinking existed
    _assert_extends(llm.requests[1], llm.requests[2])  # and the thinking that came later is stable


class _Selector(ToolSelector):
    """Keeps the tool names in ``keeps``, one set per call (the last repeats)."""

    def __init__(self, settings, *keeps: set[str]) -> None:
        super().__init__(None, settings)
        self.keeps = list(keeps)
        self.calls = 0

    async def select(self, schemas, query, recent_tool_names=()):
        self.calls += 1
        keep = self.keeps[min(self.calls, len(self.keeps)) - 1]
        return [s for s in schemas if s["name"] in keep]


def _skill_tools() -> _Tools:
    """create_event plus load_skill and a tool the skill needs."""
    tools = _Tools()

    async def load_skill(name: str = "") -> str:
        return "Budget skill: use search_drive"

    async def search_drive(query: str = "") -> str:
        return "found"

    tools.registry.register(name="load_skill", description="Load a skill", handler=load_skill)
    tools.registry.register(name="search_drive", description="Search the drive", handler=search_drive)
    return tools


_SKILL_THEN_TEXT = [
    ([thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_1", "load_skill", {"name": "budget"})], "tool_use"),
    ([text("Done.")], "end_turn"),
]
_FIRST_PICK = {"load_skill", "create_event"}


def _without_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with their thinking blocks taken out (what a strip leaves)."""
    return [
        {**m, "content": [b for b in m["content"] if b["type"] not in ("thinking", "redacted_thinking")]}
        if isinstance(m["content"], list)
        else m
        for m in messages
    ]


def _tool_names(request: dict[str, Any]) -> list[str]:
    return [t["name"] for t in request.get("tools", [])]


async def test_reselected_tools_drop_the_turns_thinking(settings, session_manager, cost_tracker):
    async with ScriptedAnthropic(settings, _SKILL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, _skill_tools().registry)
        agent.tool_selector = _Selector(settings, _FIRST_PICK, {"load_skill", "search_drive"})
        await agent.handle_message("user1", "test", "Check my budget", memory=False)

    assert len(llm.requests) == 2
    assert _tool_names(llm.requests[0]) != _tool_names(llm.requests[1])  # precondition: the tools changed
    assert "thinking" not in _block_types(llm.requests[1])
    _assert_no_sentinel("the request after re-selection", llm.raw_requests[1])


async def test_same_tools_after_reselection_keep_the_thinking(settings, session_manager, cost_tracker):
    async with ScriptedAnthropic(settings, _SKILL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, _skill_tools().registry)
        selector = _Selector(settings, _FIRST_PICK)
        agent.tool_selector = selector
        await agent.handle_message("user1", "test", "Check my budget", memory=False)

    assert selector.calls == 2  # precondition: the skill load re-selected
    assert len(llm.requests) == 2
    _assert_extends(llm.requests[0], llm.requests[1])
    assistant = [m for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"][0] == {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE}


async def test_streaming_reselected_tools_drop_the_turns_thinking(settings, session_manager, cost_tracker):
    script = [*_SKILL_THEN_TEXT, ([text("Done.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, _skill_tools().registry)
        agent.tool_selector = _Selector(settings, _FIRST_PICK, {"load_skill", "search_drive"})
        _chunks = [c async for c in agent.handle_message_stream("user1", "web", "Check my budget", memory=False)]

    assert len(llm.requests) == 3
    assert _tool_names(llm.requests[0]) != _tool_names(llm.requests[1])  # precondition: the tools changed
    for request, raw in zip(llm.requests[1:], llm.raw_requests[1:], strict=True):
        assert "thinking" not in _block_types(request)
        _assert_no_sentinel("a request after re-selection", raw)


async def test_headless_follow_up_carries_the_turns_thinking(settings, session_manager, cost_tracker, tools):
    async with ScriptedAnthropic(settings, _TOOL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        result = await agent.run_headless("Book the dentist", "user1", "telegram")

    assert result == "Erledigt."
    assert len(llm.requests) == 2
    _assert_extends(llm.requests[0], llm.requests[1])
    assistant = [m for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"][0] == {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE}


async def test_headless_reselected_tools_drop_the_turns_thinking(settings, session_manager, cost_tracker):
    async with ScriptedAnthropic(settings, _SKILL_THEN_TEXT) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, _skill_tools().registry)
        agent.tool_selector = _Selector(settings, _FIRST_PICK, {"load_skill", "search_drive"})
        await agent.run_headless("Check my budget", "user1", "telegram")

    assert len(llm.requests) == 2
    assert _tool_names(llm.requests[0]) != _tool_names(llm.requests[1])  # precondition: the tools changed
    assert "thinking" not in _block_types(llm.requests[1])
    _assert_no_sentinel("the request after re-selection", llm.raw_requests[1])


async def test_mid_turn_pair_repair_drops_the_turns_thinking(settings, session_manager, cost_tracker, tools):
    """A compatible server repeating a tool_use id makes the next iteration's repair rewrite history."""
    script = [
        ([thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_1", "create_event", {"title": "Dentist"})], "tool_use"),
        (
            [
                thinking("SECOND-THOUGHT-7f3a", "SIG-2"),
                tool_use("toolu_2", "create_event", {"title": "Doctor"}),
                tool_use("toolu_2", "create_event", {"title": "Doctor"}),
            ],
            "tool_use",
        ),
        ([text("Both booked.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tools.registry)
        result = await agent.handle_message("user1", "test", "Book both", memory=False)

    assert result.text == "Both booked."
    assert len(llm.requests) == 3
    assert "thinking" in _block_types(llm.requests[1])  # precondition: thinking rode along before the repair
    assert "thinking" not in _block_types(llm.requests[2])
    for sentinel in (*_SENTINELS, "SECOND-THOUGHT-7f3a"):
        assert sentinel not in llm.raw_requests[2]


def _failover_router(primary: BaseLLMProvider, failover: BaseLLMProvider) -> LLMRouter:
    """A router whose pool is these two providers, primary first (as in test_router.py)."""
    router = LLMRouter.__new__(LLMRouter)
    router._settings = SimpleNamespace(  # type: ignore[assignment]
        default_provider="a",
        fallback_providers=["b"],
        openai_compatible_provider="",
        anthropic_compatible_provider="",
    )
    router._pool = {"a": primary, "b": failover}
    router._on_demand = {}
    return router


async def test_failover_mid_turn_sends_the_thinking_unchanged(settings, session_manager, cost_tracker, tools):
    script_a = [
        ([thinking(_THOUGHT, _SIGNATURE), redacted(_REDACTED), tool_use("toolu_1", "create_event", {})], "tool_use"),
        400,  # provider A fails mid-turn
    ]
    async with (
        ScriptedAnthropic(settings.model_copy(update={"anthropic_model": "claude-a"}), script_a) as a,
        ScriptedAnthropic(
            settings.model_copy(update={"anthropic_model": "claude-b"}), [([text("Done.")], "end_turn")]
        ) as b,
    ):
        agent = Agent(settings, _failover_router(a.provider, b.provider), session_manager, cost_tracker, tools.registry)
        result = await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    assert result.text == "Done."
    assert [r["model"] for r in (*a.requests, *b.requests)] == ["claude-a", "claude-a", "claude-b"]  # precondition
    assert b.requests[0]["messages"] == a.requests[1]["messages"]  # the blocks went on unchanged
    assistant = [m for m in b.requests[0]["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"][:2] == [
        {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
        {"type": "redacted_thinking", "data": _REDACTED},
    ]


@pytest.mark.parametrize("loop", ["chat", "stream", "voice"])
async def test_a_failed_turn_leaves_no_turn_open(settings, session_manager, cost_tracker, tools, loop):
    """A turn that fails right after it began still ends: the session trims again on its next add."""
    agent = _voice_agent(settings, _ScriptedVoice([]), session_manager, cost_tracker, tools)
    open_when_failing: list[bool] = []

    async def failing_add(session, message):
        open_when_failing.append(session._turn_open)
        raise RuntimeError("database is locked")

    with patch.object(session_manager, "add_message", side_effect=failing_add), pytest.raises(RuntimeError):
        if loop == "chat":
            await agent.handle_message("user1", loop, "hi", memory=False)
        elif loop == "stream":
            _chunks = [c async for c in agent.handle_message_stream("user1", loop, "hi", memory=False)]
        else:
            _chunks = [c async for c in agent.stream_voice_turn("user1", loop, "hi")]

    assert open_when_failing == [True]  # precondition: it failed inside the turn
    session = await session_manager.get_or_create("user1", loop)
    assert session._turn_open is False
