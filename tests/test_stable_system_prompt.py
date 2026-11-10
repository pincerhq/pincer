"""A conversation's system prompt stays byte-identical; memories and time ride on the user message
(issue #357 slice 8, owner rulings Q14/Q15)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic_sse import ScriptedAnthropic, text
from openai import AsyncOpenAI
from pydantic import SecretStr

from pincer.core.agent import MEMORY_BLOCK_HEADER, Agent
from pincer.core.clock import current_time_block
from pincer.core.compaction import Compactor, LastRequest, choose_cut
from pincer.core.session import SessionManager
from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.tools.registry import ToolRegistry

_TIME_HEADER = "[Current date and time]"
_TURN_1_AT = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
_TURN_2_AT = datetime(2026, 11, 9, 10, 7, tzinfo=UTC)


class _Clock:
    """Stands in for ``pincer.core.clock.datetime``: ``now()`` returns the moment the test set."""

    at = _TURN_1_AT

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.at if tz is None else cls.at.astimezone(tz)


def clock_block(at: datetime, zone: str = "Europe/Berlin") -> str:
    return current_time_block(SimpleNamespace(timezone=zone), now=at)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[_Clock]:
    _Clock.at = _TURN_1_AT
    monkeypatch.setattr("pincer.core.clock.datetime", _Clock)
    return _Clock


def _memory(by_query: dict[str, str]) -> AsyncMock:
    """A memory store that recalls one memory per user text, has no profile, and stores nothing."""
    store = AsyncMock()

    async def search_text(query: str, **_: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(content=by_query[query])] if query in by_query else []

    store.search_text.side_effect = search_text
    store.list_memories.return_value = []
    return store


def _raw_system(raw_request: str) -> str:
    """The ``system`` value exactly as it went over the wire."""
    return json.dumps(json.loads(raw_request)["system"], ensure_ascii=False, separators=(",", ":"))


async def _onboarded(session_manager, user_id: str = "user1", channel: str = "test") -> None:
    await session_manager.onboarding.complete(user_id, channel, now=1.0)


async def test_two_turns_send_a_byte_identical_system_prompt(settings, session_manager, cost_tracker, clock):
    await _onboarded(session_manager)
    memory = _memory({"turn 1": "likes green tea", "turn 2": "lives in Lviv"})
    replies = [([text("Reply 1")], "end_turn"), ([text("Reply 2")], "end_turn")]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        await agent.handle_message("user1", "test", "turn 1")
        clock.at = _TURN_2_AT
        await agent.handle_message("user1", "test", "turn 2")

    first, second = (_raw_system(r) for r in llm.raw_requests)
    assert second == first
    for leaked in (_TIME_HEADER, MEMORY_BLOCK_HEADER, "likes green tea", "lives in Lviv"):
        assert leaked not in first


def _raw_messages(raw_request: str) -> list[str]:
    """Each message exactly as it went over the wire."""
    return [json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in json.loads(raw_request)["messages"]]


async def test_the_user_message_carries_its_context_and_replays_it_unchanged(
    settings, session_manager, cost_tracker, clock
):
    await _onboarded(session_manager)
    memory = _memory({"turn 1": "likes green tea", "turn 2": "lives in Lviv"})
    replies = [([text("Reply 1")], "end_turn"), ([text("Reply 2")], "end_turn")]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        await agent.handle_message("user1", "test", "turn 1")
        clock.at = _TURN_2_AT
        await agent.handle_message("user1", "test", "turn 2")

    newest = llm.requests[1]["messages"][-1]
    assert newest["role"] == "user"
    assert isinstance(newest["content"], list), newest["content"]
    context, words = newest["content"]
    assert words == {"type": "text", "text": "turn 2"}
    assert context["type"] == "text"
    assert context["text"].startswith(f"{MEMORY_BLOCK_HEADER}\n- lives in Lviv\n\n{_TIME_HEADER}\n")
    assert "Monday, 09 November 2026, 11:07 (CET, Europe/Berlin)" in context["text"]
    assert _raw_messages(llm.raw_requests[1])[0] == _raw_messages(llm.raw_requests[0])[0]


async def test_the_context_replays_unchanged_after_a_reload_from_storage(
    settings, session_manager, cost_tracker, clock, tmp_path
):
    await _onboarded(session_manager)
    memory = _memory({"turn 1": "likes green tea"})
    replies = [([text("Reply 1")], "end_turn"), ([text("Reply 2")], "end_turn")]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        await agent.handle_message("user1", "test", "turn 1")

        reloaded = SessionManager(tmp_path / "test.db", max_messages=20)
        await reloaded.initialize()
        try:
            clock.at = _TURN_2_AT
            agent = Agent(settings, llm.provider, reloaded, cost_tracker, ToolRegistry(), memory_store=memory)
            await agent.handle_message("user1", "test", "turn 2")
        finally:
            await reloaded.close()

    turn_1_sent = _raw_messages(llm.raw_requests[0])[0]
    assert "likes green tea" in turn_1_sent
    assert _raw_messages(llm.raw_requests[1])[0] == turn_1_sent


async def test_an_old_session_without_context_loads_and_replays_as_before(
    settings, session_manager, cost_tracker, clock
):
    """Sessions saved before the context block (no ``context`` key) need no migration."""
    await _onboarded(session_manager)
    old = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"}]
    await session_manager._store.save(
        session_id="test:user1",
        user_id="user1",
        channel="test",
        messages=json.dumps(old),
        metadata="{}",
        created_at=1.0,
        updated_at=1.0,
    )
    async with ScriptedAnthropic(settings, [([text("Reply")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
        await agent.handle_message("user1", "test", "turn 2")

    sent = _raw_messages(llm.raw_requests[0])
    assert sent[:2] == ['{"role":"user","content":"hi"}', '{"role":"assistant","content":"Hello!"}']


class _ScriptedOpenAI:
    """An OpenAI provider whose completions are scripted text replies; keeps each request body."""

    def __init__(self, settings, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.provider = OpenAICompatibleProvider(
            settings.model_copy(update={"openai_model": "gpt-4o", "openai_api_key": SecretStr("sk-test")}), "openai"
        )
        self.provider._client = AsyncOpenAI(
            api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": self._replies.pop(0)}
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o",
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


async def test_openai_gets_the_context_as_a_text_part_before_the_words(settings, session_manager, cost_tracker, clock):
    await _onboarded(session_manager)
    llm = _ScriptedOpenAI(settings, ["Reply 1", "Reply 2"])
    agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    clock.at = _TURN_2_AT
    await agent.handle_message("user1", "test", "turn 2", memory=False)
    await llm.provider.close()

    first, second = (r["messages"] for r in llm.requests)
    assert second[0] == first[0]  # the system prompt
    assert first[0]["role"] == "system"
    assert _TIME_HEADER not in first[0]["content"]
    assert second[1] == first[1]  # turn 1, replayed with its own context
    assert isinstance(second[-1]["content"], list), second[-1]["content"]
    context, words = second[-1]["content"]
    assert words == {"type": "text", "text": "turn 2"}
    assert context["type"] == "text"
    assert context["text"].startswith(f"{_TIME_HEADER}\nMonday, 09 November 2026, 11:07 (CET, Europe/Berlin)\n")


async def test_web_chat_turns_send_one_system_prompt_and_the_context_on_the_user_message(
    settings, session_manager, cost_tracker, clock
):
    await _onboarded(session_manager)
    memory = _memory({"turn 1": "likes green tea", "turn 2": "lives in Lviv"})
    replies = [([text(f"Reply {n}")], "end_turn") for n in (1, 2)]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        for turn, at in (("turn 1", _TURN_1_AT), ("turn 2", _TURN_2_AT)):
            clock.at = at
            chunks = [c async for c in agent.handle_message_stream("user1", "test", turn)]
            assert chunks[-1].content == f"Reply {turn[-1]}"

    assert len({_raw_system(r) for r in llm.raw_requests}) == 1
    newest = llm.requests[-1]["messages"][-1]
    assert isinstance(newest["content"], list), newest["content"]
    context, words = newest["content"]
    assert words == {"type": "text", "text": "turn 2"}
    assert context["text"].startswith(f"{MEMORY_BLOCK_HEADER}\n- lives in Lviv\n\n{_TIME_HEADER}\n")
    assert len(llm.raw_requests) == 2
    assert _raw_messages(llm.raw_requests[1])[0] == _raw_messages(llm.raw_requests[0])[0]


async def test_each_voice_utterance_carries_its_own_context_under_one_system_prompt(
    settings, session_manager, cost_tracker, clock
):
    memory = _memory({"first": "likes green tea", "second": "lives in Lviv"})
    replies = [([text("Answer 1")], "end_turn"), ([text("Answer 2")], "end_turn")]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        for utterance, at in (("first", _TURN_1_AT), ("second", _TURN_2_AT)):
            clock.at = at
            stream = agent.stream_voice_turn(
                "user1", "voice", utterance, extra_system="CALL RULES", session_key="voice:call-1"
            )
            [c async for c in stream]

    first, second = (_raw_system(r) for r in llm.raw_requests)
    assert second == first
    assert "CALL RULES" in first
    sent = llm.requests[1]["messages"]
    assert isinstance(sent[-1]["content"], list), sent[-1]["content"]
    assert [block["text"] for block in sent[-1]["content"]] == [
        f"{MEMORY_BLOCK_HEADER}\n- lives in Lviv\n\n{clock_block(_TURN_2_AT)}",
        "second",
    ]
    assert _raw_messages(llm.raw_requests[1])[0] == _raw_messages(llm.raw_requests[0])[0]


async def test_a_headless_run_keeps_the_time_in_its_own_system_prompt(settings, session_manager, cost_tracker, clock):
    """Scheduled runs have no session to cache: their prompt is the one they had before slice 8."""
    async with ScriptedAnthropic(settings, [([text("Done")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
        assert await agent.run_headless("check the weather", "user1", "test") == "Done"

    request = llm.requests[0]
    assert request["system"].endswith("\n\n" + clock_block(_TURN_1_AT))
    assert request["messages"] == [{"role": "user", "content": "check the weather"}]


async def test_web_chat_history_shows_the_users_words_without_the_context(
    settings, session_manager, cost_tracker, clock, tmp_path
):
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app
    from pincer.memory.sqlite import SqlMemoryBackend

    memory = SqlMemoryBackend(tmp_path / "test.db")
    await memory.initialize()
    await memory.store_memory(user_id="user", content="The user drinks green tea every morning", category="fact")
    await _onboarded(session_manager, "user", "web")
    async with ScriptedAnthropic(settings, [([text("Noted")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        app = create_app()
        app.state.agent = agent
        app.dependency_overrides[chat_user] = lambda: "user"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            sent = await client.post("/api/chat/message", json={"text": "green tea"})
            assert sent.status_code == 200
            history = await client.get("/api/conversations", params={"user_id": "user"})
    await memory.close()

    context = llm.requests[0]["messages"][-1]["content"][0]["text"]
    assert MEMORY_BLOCK_HEADER in context  # the turn had memories and time to hide
    assert history.status_code == 200
    [conversation] = history.json()["conversations"]
    assert conversation["messages"][0] == {"role": "user", "content": "green tea"}
    for hidden in (_TIME_HEADER, MEMORY_BLOCK_HEADER, "every morning"):
        assert hidden not in history.text


async def test_a_compaction_summarizes_the_users_words_without_their_context(
    settings, session_manager, cost_tracker, clock
):
    session = await session_manager.get_or_create("user1", "test")
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        words = f"message {index} " + "x" * 40_000
        context = "CONTEXT-NOT-FOR-SUMMARY" if role == MessageRole.USER else ""
        await session_manager.add_message(session, LLMMessage(role=role, content=words, context=context))
    reply = ([text("Reply")], "end_turn", {"cache_read_input_tokens": 100_000})
    async with (
        ScriptedAnthropic(settings, [reply]) as llm,
        ScriptedAnthropic(settings, [([text("SUMMARY")], "end_turn")]) as summary_llm,
    ):
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
        )
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
        await agent.handle_message("user1", "test", "turn", memory=False)
        await agent.wait_compactions()

    [summary_request] = summary_llm.raw_requests
    assert "message 0 " in summary_request  # the words were summarized
    assert "CONTEXT-NOT-FOR-SUMMARY" not in summary_request


def test_the_cut_counts_the_context_as_part_of_the_prompt():
    """~20K tokens over 20K characters, 18K of them context: 1 token a character. Without the
    context the two old messages would look ten times heavier and one would be cut, too few."""
    messages = [
        LLMMessage(role=MessageRole.USER, content="u" * 1_000),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 1_000),
        LLMMessage(role=MessageRole.USER, content="q", context="c" * 18_000),
        LLMMessage(role=MessageRole.ASSISTANT, content="r"),
    ]
    last = LastRequest.measure(20_000, messages, overhead_chars=0)

    assert choose_cut(messages, last, target_tokens=20_500) == 2


async def test_a_skill_installed_mid_conversation_is_in_the_next_turns_system_prompt(
    settings, session_manager, cost_tracker, clock, tmp_path
):
    from pincer.services.skills import SkillService

    user_skills = tmp_path / "user-skills"
    user_skills.mkdir()
    skills = SkillService(bundled_dir=None, user_dir=user_skills, cache_dir=tmp_path / "skills-cache")
    await _onboarded(session_manager)
    replies = [([text("Reply 1")], "end_turn"), ([text("Reply 2")], "end_turn")]
    async with ScriptedAnthropic(settings, replies) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
        agent.skill_service = skills
        await agent.handle_message("user1", "test", "turn 1", memory=False)
        installed = user_skills / "brew-tea" / "SKILL.md"
        installed.parent.mkdir()
        installed.write_text("---\nname: brew-tea\ndescription: Brews green tea\n---\nbody\n", encoding="utf-8")
        skills.rebuild()
        await agent.handle_message("user1", "test", "turn 2", memory=False)

    first, second = (r["system"] for r in llm.requests)
    assert "brew-tea" not in first
    assert "brew-tea" in second


async def test_a_memory_free_turn_sends_the_time_only_after_the_history_summary(
    settings, session_manager, cost_tracker, clock
):
    """No memory search in a memory-free session; the slice 9 summary merges in before the context."""
    session = await session_manager.get_or_create("user1", "test")
    summary = LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nTalked about tea.")
    await session_manager.add_message(session, summary)
    memory = _memory({"turn 1": "likes green tea"})
    async with ScriptedAnthropic(settings, [([text("Reply")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        await agent.handle_message("user1", "test", "turn 1", memory=False)

    memory.search_text.assert_not_awaited()
    assert llm.requests[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[Previous conversation summary]\nTalked about tea."},
                {"type": "text", "text": clock_block(_TURN_1_AT)},
                {"type": "text", "text": "turn 1"},
            ],
        }
    ]


class _OverlapMemory:
    """A memory store whose search and profile read each wait (up to 1 s) for the other to start,
    and record whether it did: both True only when the two run concurrently."""

    def __init__(self) -> None:
        self.search_started = asyncio.Event()
        self.language_started = asyncio.Event()
        self.overlap = {"search": False, "language": False}

    @staticmethod
    async def _saw(event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout=1.0)
        except TimeoutError:
            return False
        return True

    async def search_text(self, query: str, **_: Any) -> list[SimpleNamespace]:
        self.search_started.set()
        self.overlap["search"] = await self._saw(self.language_started)
        return [SimpleNamespace(content="likes green tea")]

    async def list_memories(self, **kwargs: Any) -> list[SimpleNamespace]:
        if kwargs.get("category") != "profile":
            return []
        self.language_started.set()
        self.overlap["language"] = await self._saw(self.search_started)
        return [SimpleNamespace(id="p1", content="Preferred language: uk.")]

    async def store_memory(self, **_: Any) -> None:
        return None


@pytest.mark.parametrize("streaming", [False, True], ids=["chat", "web-chat"])
async def test_the_memory_search_and_the_language_read_run_concurrently(
    settings, session_manager, cost_tracker, clock, streaming
):
    await _onboarded(session_manager)
    memory = _OverlapMemory()
    async with ScriptedAnthropic(settings, [([text("Reply")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), memory_store=memory)
        if streaming:
            [c async for c in agent.handle_message_stream("user1", "test", "turn 1")]
        else:
            await agent.handle_message("user1", "test", "turn 1")

    assert memory.overlap == {"search": True, "language": True}
    request = llm.requests[0]
    assert "[User preferred language: uk]" in json.dumps(request["system"])
    assert request["messages"][-1]["content"][0]["text"].startswith(f"{MEMORY_BLOCK_HEADER}\n- likes green tea\n\n")
