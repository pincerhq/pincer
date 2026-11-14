"""OpenAI prompt caching: one `prompt_cache_key` per session, and a tool list that only grows
(issue #357 slice 11, owner rulings Q18/Q21)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai_wire import CALL as _CALL
from openai_wire import ScriptedOpenAI
from pydantic import SecretStr

from pincer.core.agent import Agent
from pincer.core.compaction import Compactor
from pincer.llm.base import LLMMessage, MessageRole, prompt_cache_session
from pincer.tools.registry import ToolRegistry
from pincer.tools.selection import ToolSelector


@pytest.fixture(params=["chat_completions", "responses"])
def api(request) -> str:
    """Every test that takes it runs on Chat Completions and on the Responses API (issue #358): the cache
    key and the tool list must behave the same on both."""
    return request.param


def _on(api: str, settings, *names: str):
    """`settings` with the Responses API on for `names` (and the default endpoints) or on for none."""
    listed = [] if api == "chat_completions" else [*settings.responses_api_providers, *names]
    return settings.model_copy(update={"responses_api_providers": listed})


@pytest.fixture
def openai_settings(settings, api):
    return _on(
        api,
        settings.model_copy(
            update={
                "default_provider": "openai",
                "openai_model": "gpt-4o",
                "openai_api_key": SecretStr("sk-test"),
            }
        ),
    )


async def _onboarded(session_manager, user_id: str = "user1", channel: str = "test") -> None:
    await session_manager.onboarding.complete(user_id, channel, now=1.0)


async def test_every_request_of_a_session_carries_the_same_uuid7_cache_key(
    openai_settings, session_manager, cost_tracker
):
    await _onboarded(session_manager)
    llm = ScriptedOpenAI(openai_settings, ["Reply 1", "Reply 2"])
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    await agent.handle_message("user1", "test", "turn 2", memory=False)

    first, second = (r.get("prompt_cache_key") for r in llm.requests)
    assert first is not None
    assert second == first
    assert len(first) == 36
    assert uuid.UUID(first).version == 7
    assert str(uuid.UUID(first)) == first  # canonical, lowercase
    session = await session_manager.get_or_create("user1", "test")
    for private in (session.session_id, "user1"):
        assert private not in first


async def test_different_sessions_get_different_cache_keys(openai_settings, session_manager, cost_tracker):
    await _onboarded(session_manager, "user1")
    await _onboarded(session_manager, "user2")
    llm = ScriptedOpenAI(openai_settings, ["Reply 1", "Reply 2"])
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
    await agent.handle_message("user1", "test", "hello", memory=False)
    await agent.handle_message("user2", "test", "hello", memory=False)

    first, second = (r["prompt_cache_key"] for r in llm.requests)
    assert first != second


async def test_the_stream_and_voice_paths_send_their_sessions_cache_key(openai_settings, session_manager, cost_tracker):
    await _onboarded(session_manager)
    llm = ScriptedOpenAI(openai_settings, ["Reply 1", "Reply 2", "Answer 1", "Answer 2"])
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    [c async for c in agent.handle_message_stream("user1", "test", "turn 2", memory=False)]
    for utterance in ("first", "second"):
        [c async for c in agent.stream_voice_turn("user1", "voice", utterance, session_key="voice:call-1")]

    keys = [r.get("prompt_cache_key") for r in llm.requests]
    assert len(keys) == 4  # a web chat turn without tool calls is one streamed request
    chat_key = keys[0]
    assert chat_key
    assert keys[1] == chat_key  # the web chat turn's streamed request
    voice_key = keys[2]
    assert voice_key
    assert voice_key != chat_key  # a call is a session of its own
    assert keys[3] == voice_key


def _compatible(settings, api: str, name: str, api_key: str, **update: Any):
    """Settings whose OpenAI-compatible endpoint is called `name` (on `api`) and has `api_key` ("" = keyless)."""
    return _on(
        api,
        settings.model_copy(
            update={
                "openai_compatible_provider": name,
                "openai_compatible_base_url": "http://127.0.0.1:8000/v1",
                "openai_compatible_api_key": SecretStr(api_key),
                "openai_compatible_model": "gpt-4o",
                **update,
            }
        ),
        name,
    )


async def _sends_cache_key(settings, provider: str) -> bool:
    llm = ScriptedOpenAI(settings, ["Hi"], provider=provider)
    with prompt_cache_session(lambda: "0190a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"):
        await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hello")])
    [request] = llm.requests
    return "prompt_cache_key" in request


@pytest.mark.parametrize(
    ("name", "api_key", "sends_key"),
    [("azure-openai", "", False), ("groq", "sk-groq", False), ("azure-openai", "sk-azure", True)],
    ids=["listed-but-keyless", "keyed-but-unlisted", "listed-and-keyed"],
)
async def test_only_listed_providers_get_the_cache_key(settings, api, name, api_key, sends_key):
    """Strict endpoints (Groq, DeepSeek, a vLLM proxy) reject the unknown field with a 400, and a keyless
    local server has no paid cache to key: by default only openai and Azure OpenAI get it."""
    assert await _sends_cache_key(_compatible(settings, api, name, api_key), name) is sends_key


async def test_a_provider_listed_in_the_setting_gets_the_cache_key(settings, api):
    """A proxy that does accept the key, or an Azure endpoint named differently, opts in by name."""
    opted_in = _compatible(settings, api, "groq", "sk-groq", prompt_cache_key_providers=["openai", "groq"])
    assert await _sends_cache_key(opted_in, "groq") is True


def test_the_cache_key_providers_come_from_a_comma_separated_env_list(monkeypatch, tmp_path):
    from pincer.config import Settings

    default = Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path, _env_file=None)  # type: ignore[arg-type,call-arg]
    assert default.prompt_cache_key_providers == ["openai", "azure", "azure-openai"]
    monkeypatch.setenv("PINCER_PROMPT_CACHE_KEY_PROVIDERS", "openai, vllm")
    listed = Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path, _env_file=None)  # type: ignore[arg-type,call-arg]
    assert listed.prompt_cache_key_providers == ["openai", "vllm"]


async def test_the_background_compaction_summary_carries_no_cache_key(openai_settings, session_manager, cost_tracker):
    """The summary is a one-shot request of its own, run after the turn: it is not part of the conversation."""
    await _onboarded(session_manager)
    session = await session_manager.get_or_create("user1", "test")
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(session, LLMMessage(role=role, content=f"message {index} " + "x" * 40_000))
    llm = ScriptedOpenAI(openai_settings, ["Reply"], prompt_tokens=100_000)
    summary_llm = ScriptedOpenAI(openai_settings, ["The summary."])
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
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, ToolRegistry(), compactor=compactor)
    await agent.handle_message("user1", "test", "turn", memory=False)
    await agent.wait_compactions()

    assert llm.requests[0]["prompt_cache_key"]
    [summary] = summary_llm.requests  # precondition: the compaction ran
    assert "prompt_cache_key" not in summary


async def test_the_cache_key_survives_a_reload(openai_settings, session_manager, cost_tracker):
    await _onboarded(session_manager)
    llm = ScriptedOpenAI(openai_settings, ["Reply 1", "Reply 2"])
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, ToolRegistry())
    await agent.handle_message("user1", "test", "turn 1", memory=False)
    before = await session_manager.get_or_create("user1", "test")
    await session_manager.evict("test:user1")
    await agent.handle_message("user1", "test", "turn 2", memory=False)

    assert await session_manager.get_or_create("user1", "test") is not before  # precondition: loaded from storage
    first, second = (r["prompt_cache_key"] for r in llm.requests)
    assert second == first


# ── The conversation's tool list ─────────────────────────────────────────────

_TOOLS = [f"t{i}" for i in range(10)]
# What the tool index ranks first for each turn's words
_RANKING = {"alpha": ["t5", "t6"], "beta": ["t8", "t2"], "gamma": ["t9", "t3"]}


class _Index:
    """The memory server's tool index (tool_index_sync / tool_index_search), ranking by the turn's words."""

    def __init__(self) -> None:
        self.tools = [SimpleNamespace(name=n) for n in ("tool_index_sync", "tool_index_search")]
        self.version = ""

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "tool_index_sync":
            self.version = args["version"]
            data: dict[str, Any] = {"version": self.version, "added": len(args["tools"])}
        else:
            found = [(args["query"].find(word), r) for word, r in _RANKING.items() if word in args["query"]]
            ranking = min(found)[1] if found else []  # the word that comes first in the query
            data = {"stale": False, "results": [{"name": n, "distance": 0.1} for n in ranking]}
        return SimpleNamespace(structuredContent=data, isError=False, content=[])


class _Manager:
    def __init__(self) -> None:
        self.index = _Index()

    async def get_session(self, name: str) -> _Index:
        return self.index

    def tool_names(self, name: str) -> list[str]:
        return []


@pytest.fixture
def capped_settings(openai_settings):
    """Seven tools at most, three of them kept free: a fresh selection sends four."""
    return openai_settings.model_copy(
        update={"tool_selection_max": 7, "tool_selection_headroom": 3, "tool_selection_default": []}
    )


def _capped_agent(settings, session_manager, cost_tracker, replies: list[str]) -> tuple[Agent, ScriptedOpenAI]:
    registry = ToolRegistry()
    for name in _TOOLS:

        async def handler() -> str:
            return "ok"

        registry.register(name=name, description=f"{name} tool", handler=handler)

    async def load_skill(name: str = "") -> str:
        return "Planning skill: use the gamma tools"

    registry.register(name="load_skill", description="Load a skill", handler=load_skill)
    llm = ScriptedOpenAI(settings, replies)
    agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
    agent.tool_selector = ToolSelector(cast("Any", _Manager()), settings)
    return agent, llm


def _sent_tools(request: dict[str, Any]) -> list[str]:
    # Nested under "function" in Chat Completions, flat in Responses
    return [t["name"] if "name" in t else t["function"]["name"] for t in request["tools"]]


async def test_each_turn_appends_its_new_tools_after_the_earlier_ones(capped_settings, session_manager, cost_tracker):
    await _onboarded(session_manager)
    agent, llm = _capped_agent(capped_settings, session_manager, cost_tracker, ["Reply 1", "Reply 2"])
    await agent.handle_message("user1", "test", "alpha", memory=False)
    await agent.handle_message("user1", "test", "beta", memory=False)

    first, second = (_sent_tools(r) for r in llm.requests)
    assert first == ["t0", "t1", "t5", "t6"]  # a fresh selection: ranked, then registry order, to 7 - 3
    assert second == [*first, "t8", "t2"]


async def test_a_list_that_would_pass_the_cap_is_selected_afresh(capped_settings, session_manager, cost_tracker):
    await _onboarded(session_manager)
    agent, llm = _capped_agent(capped_settings, session_manager, cost_tracker, ["Reply 1", "Reply 2", "Reply 3"])
    for words in ("alpha", "beta", "gamma"):
        await agent.handle_message("user1", "test", words, memory=False)

    second, third = (_sent_tools(r) for r in llm.requests[1:])
    assert len(second) == 6  # gamma's two new tools would make 8, past 7
    assert third == ["t0", "t1", "t3", "t9"]  # fresh, back to 7 - 3


async def test_the_kept_tool_list_survives_a_reload(capped_settings, session_manager, cost_tracker):
    await _onboarded(session_manager)
    agent, llm = _capped_agent(capped_settings, session_manager, cost_tracker, ["Reply 1", "Reply 2"])
    await agent.handle_message("user1", "test", "alpha", memory=False)
    before = await session_manager.get_or_create("user1", "test")
    await session_manager.evict("test:user1")
    await agent.handle_message("user1", "test", "beta", memory=False)

    assert await session_manager.get_or_create("user1", "test") is not before  # precondition: loaded from storage
    first, second = (_sent_tools(r) for r in llm.requests)
    assert second == [*first, "t8", "t2"]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_skill_loaded_mid_turn_appends_its_tools_to_the_list(
    capped_settings, session_manager, cost_tracker, streaming
):
    await _onboarded(session_manager)
    replies = [f"{_CALL}load_skill", "Planned.", "Planned."]
    agent, llm = _capped_agent(capped_settings, session_manager, cost_tracker, replies)
    if streaming:
        [c async for c in agent.handle_message_stream("user1", "test", "alpha", memory=False)]
    else:
        await agent.handle_message("user1", "test", "alpha", memory=False)

    before, after = (_sent_tools(r) for r in llm.requests[:2])
    assert before == ["t0", "t1", "t5", "t6"]
    # The tool just called, then the skill's tools, after the turn's first ones
    assert after == [*before, "load_skill", "t9", "t3"]
    session = await session_manager.get_or_create("user1", "test")
    assert json.loads(session.metadata["tool_list"]) == after


@pytest.mark.parametrize(
    "update",
    [{"tool_selection_max": 127, "tool_selection_headroom": 27}, {"default_provider": "anthropic"}],
    ids=["under-the-cap", "anthropic-only"],
)
async def test_without_selection_every_tool_is_sent_and_no_list_is_kept(
    capped_settings, session_manager, cost_tracker, update
):
    await _onboarded(session_manager)
    settings = capped_settings.model_copy(update=update)
    agent, llm = _capped_agent(settings, session_manager, cost_tracker, ["Reply 1", "Reply 2"])
    await agent.handle_message("user1", "test", "alpha", memory=False)
    await agent.handle_message("user1", "test", "beta", memory=False)

    for request in llm.requests:
        assert _sent_tools(request) == [*_TOOLS, "load_skill"]
    session = await session_manager.get_or_create("user1", "test")
    assert "tool_list" not in session.metadata
