"""OpenAI and Azure on the Responses API, for `complete()` (issue #358, slice 6).

A real OpenAI SDK client on a MockTransport (tests/openai_wire.py): the SDK's own request building and
reply parsing run, and every request body and URL is recorded.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai_wire import (
    FIXTURES,
    GOLDEN_SCENARIOS,
    GOLDEN_TOOLS,
    PNG_1X1,
    Incomplete,
    Reply,
    ScriptedOpenAI,
    function_call_item,
    golden_chat_request,
    message_item,
    reasoning_item,
    usage,
)
from pydantic import SecretStr
from sqlmodel import select

from pincer.config.pricing import TokenUsage
from pincer.core.agent import Agent
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import ImageContent, LLMMessage, MessageRole, ToolCall
from pincer.models.costs import CostLog
from pincer.tools.registry import ToolRegistry

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


async def test_a_listed_endpoint_posts_the_responses_request(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [[message_item("Hello!")]])
    result = await llm.provider.complete(_HI, system="You are Pincer.")

    assert llm.paths == ["/v1/responses"]  # precondition: the request went to the Responses API at all
    assert llm.requests == [
        {
            "model": "gpt-5.2",
            "instructions": "You are Pincer.",
            "input": [{"role": "user", "content": "hi"}],
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": 4096,
        }
    ]
    assert result.content == "Hello!"


# ── Which endpoint uses which API ────────────────────────────────────────────


def _compatible(settings, name: str, **update: Any):
    """Settings whose OpenAI-compatible endpoint is called `name`, effort/thinking pinned empty."""
    return settings.model_copy(
        update={
            "openai_compatible_provider": name,
            "openai_compatible_base_url": "http://127.0.0.1:8000/v1",
            "openai_compatible_api_key": SecretStr("sk-test"),
            "openai_compatible_model": "gpt-5.2",
            "openai_model": "gpt-5.2",
            "reasoning_effort": "",
            "thinking": "",
            **update,
        }
    )


@pytest.mark.parametrize("scenario", sorted(GOLDEN_SCENARIOS))
async def test_an_unlisted_endpoint_sends_the_chat_completions_body_it_always_did(settings, scenario):
    """The golden bodies were captured from commit 6ac3e0e, before the Responses API: system, summary,
    context + photo + words, a tool call and its result, a cache key and (for the reasoning model) effort."""
    path, body = await golden_chat_request(settings, scenario)

    assert path == "/v1/chat/completions"  # precondition: the endpoint is not on the Responses API
    assert body == (FIXTURES / f"openai_chat_body_6ac3e0e_{scenario}.json").read_text()


async def _path_of(settings, provider: str) -> str:
    llm = ScriptedOpenAI(settings, [[message_item("ok")]], provider=provider)
    await llm.provider.complete(_HI)
    [path] = llm.paths
    return path


async def test_the_list_decides_which_endpoints_use_the_responses_api(settings):
    groq = _compatible(settings, "groq")
    assert await _path_of(groq, "groq") == "/v1/chat/completions"
    listed = groq.model_copy(update={"responses_api_providers": ["openai", "groq"]})
    assert await _path_of(listed, "groq") == "/v1/responses"

    openai = _compatible(settings, "groq", openai_api_key=SecretStr("sk-test"))  # also has the `openai` provider
    assert await _path_of(openai, "openai") == "/v1/responses"
    unlisted = openai.model_copy(update={"responses_api_providers": ["azure"]})
    assert await _path_of(unlisted, "openai") == "/v1/chat/completions"


def test_the_responses_providers_come_from_a_comma_separated_env_list(monkeypatch, tmp_path):
    from pincer.config import Settings

    def load() -> list[str]:
        return Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path, _env_file=None).responses_api_providers  # type: ignore[arg-type,call-arg]

    assert load() == ["openai", "azure", "azure-openai"]
    monkeypatch.setenv("PINCER_RESPONSES_API_PROVIDERS", "openai, vllm")
    assert load() == ["openai", "vllm"]
    # An empty variable is ignored by the settings loader, so "none" is how the environment turns the list off
    monkeypatch.setenv("PINCER_RESPONSES_API_PROVIDERS", "None")
    assert load() == []


# ── Tools ────────────────────────────────────────────────────────────────────


async def test_tools_go_in_the_flat_responses_format_without_strict_mode(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [[message_item("ok")]])
    await llm.provider.complete(_HI, tools=GOLDEN_TOOLS)

    assert llm.paths == ["/v1/responses"]
    # Not nested under "function" as in Chat Completions, and `strict` explicit: when it is left out the
    # Responses API tries to rewrite a schema into strict mode (migrate-to-responses guide)
    assert llm.requests[0]["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            "strict": False,
        },
        {
            "type": "function",
            "name": "note",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        },
    ]


async def test_only_the_first_128_tools_are_sent_and_the_rest_are_named_in_a_warning(openai_settings, caplog):
    tools = [{"name": f"t{i}", "input_schema": {"type": "object"}} for i in range(130)]
    llm = ScriptedOpenAI(openai_settings, [[message_item("ok")]])
    with caplog.at_level("WARNING", logger="pincer.llm.openai_common"):
        await llm.provider.complete(_HI, tools=tools)

    assert llm.paths == ["/v1/responses"]
    assert [t["name"] for t in llm.requests[0]["tools"]] == [f"t{i}" for i in range(128)]
    assert "130 tools exceed OpenAI's 128-tool limit" in caplog.text
    assert "dropping: t128, t129" in caplog.text


# ── The reply ────────────────────────────────────────────────────────────────


async def test_the_reply_keeps_its_output_items_verbatim_and_fills_text_and_tool_calls(openai_settings):
    items = [
        reasoning_item("rs_1", "ENC-1"),
        message_item("Checking.", "msg_1"),
        function_call_item("get_weather", {"city": "Kyiv"}, call_id="call_9", item_id="fc_9"),
    ]
    llm = ScriptedOpenAI(openai_settings, [items])
    result = await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    assert result.raw == items  # exactly what the API sent: the encrypted reasoning, ids, statuses
    assert result.wire == "openai:gpt-5.2"
    assert result.content == "Checking."
    assert result.tool_calls == [ToolCall(id="call_9", name="get_weather", arguments={"city": "Kyiv"})]
    assert result.stop_reason == "tool_calls"
    assert (result.provider, result.request_model) == ("openai", "gpt-5.2")


async def test_a_reply_cut_off_by_the_output_limit_is_truncated(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [Incomplete([message_item("A long ans")])])
    result = await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    assert result.truncated
    assert result.content == "A long ans"


# ── Usage ────────────────────────────────────────────────────────────────────


async def test_the_input_is_split_into_uncached_and_cached_and_the_output_keeps_its_reasoning(openai_settings):
    """1,000 input tokens of which 600 were read from the cache, 200 output tokens (reasoning included)."""
    llm = ScriptedOpenAI(openai_settings, [([message_item("ok")], usage(1000, 200, cached=600, reasoning=150))])
    result = await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (400, 600, 200)
    assert (result.cache_write_5m_tokens, result.cache_write_1h_tokens) == (0, 0)


async def test_tokens_written_to_the_cache_come_out_of_the_input_too(openai_settings):
    """`cached_tokens` counts billed reads and excludes tokens charged as writes (prompt-caching guide)."""
    llm = ScriptedOpenAI(openai_settings, [([message_item("ok")], usage(1000, 200, cached=600, written=100))])
    result = await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    assert result.usage == TokenUsage(
        input_tokens=300, cache_write_5m_tokens=100, cache_write_1h_tokens=0, cache_read_tokens=600, output_tokens=200
    )


async def test_the_reasoning_count_and_mode_are_logged_at_debug_and_never_the_items(openai_settings, caplog):
    reply = ([reasoning_item("rs_1", "ENC-SECRET-91"), message_item("ok")], usage(1000, 200, cached=600, reasoning=150))
    extras = {"reasoning": {"effort": None, "summary": None, "context": "current_turn"}}
    llm = ScriptedOpenAI(openai_settings, [reply], extras=extras)
    with caplog.at_level("DEBUG", logger="pincer.llm.openai_common"):
        await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    [line] = [r.getMessage() for r in caplog.records if "reasoning context" in r.getMessage()]
    assert line == "openai gpt-5.2: 200 output tokens, of which 150 reasoning; reasoning context current_turn"
    assert "ENC-SECRET-91" not in caplog.text


# ── Reasoning goes back: within a turn, across turns, after a reload ─────────

_FIRST = [reasoning_item("rs_1", "ENC-1"), function_call_item("lookup", {"q": "tea"}, "call_1", "fc_1")]
_SECOND = [reasoning_item("rs_2", "ENC-2"), message_item("Green tea.", "msg_2")]
_THIRD = [reasoning_item("rs_3", "ENC-3"), message_item("Anything else?", "msg_3")]


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


async def test_a_tool_turns_second_request_carries_the_reasoning_before_the_call_and_its_result(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete("user1", "test", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND])
    await agent.handle_message("user1", "test", "Which tea?", memory=False)

    assert llm.paths == ["/v1/responses", "/v1/responses"]  # precondition: a tool turn made two requests
    second = llm.requests[1]
    assert _kinds(second) == ["user", "reasoning", "function_call", "function_call_output"]
    assert second["input"][1:3] == _FIRST  # the API's own items, encrypted reasoning included, in its order
    assert second["input"][3] == {"type": "function_call_output", "call_id": "call_1", "output": "found tea"}


async def test_every_later_turn_carries_the_earlier_turns_reasoning_even_after_a_reload(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete("user1", "test", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND, _THIRD, _SECOND])
    await agent.handle_message("user1", "test", "Which tea?", memory=False)
    await agent.handle_message("user1", "test", "And coffee?", memory=False)

    assert llm.paths == ["/v1/responses"] * 3  # precondition: three requests so far
    turn_2 = llm.requests[2]["input"]
    assert [item.get("type") or item["role"] for item in turn_2] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "message",
        "user",
    ]
    assert turn_2[1:3] == _FIRST
    assert turn_2[4:6] == _SECOND

    stored = (await session_manager.get_or_create("user1", "test")).messages
    saved = [m.to_dict() for m in stored if m.role == MessageRole.ASSISTANT]
    assert [(m["wire"], [i["type"] for i in m["raw"]]) for m in saved] == [
        ("openai:gpt-5.2", ["reasoning", "function_call"]),
        ("openai:gpt-5.2", ["reasoning", "message"]),
        ("openai:gpt-5.2", ["reasoning", "message"]),
    ]

    before = await session_manager.get_or_create("user1", "test")
    await session_manager.evict("test:user1")
    await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert await session_manager.get_or_create("user1", "test") is not before  # precondition: loaded from storage
    turn_3 = llm.requests[3]["input"]
    assert turn_3[1:3] == _FIRST and turn_3[4:6] == _SECOND
    assert [i["id"] for i in turn_3 if i.get("type") == "reasoning"] == ["rs_1", "rs_2", "rs_3"]


# ── Messages that are not replayed ───────────────────────────────────────────


def _history(raw: list[dict[str, Any]], wire: str) -> list[LLMMessage]:
    call = ToolCall(id="call_1", name="lookup", arguments={"q": "tea"})
    return [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content="Checking.", tool_calls=[call], raw=raw, wire=wire),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="found", tool_call_id="call_1"),
        LLMMessage(role=MessageRole.ASSISTANT, content="Done.", raw=raw, wire=wire),
        LLMMessage(role=MessageRole.USER, content="more"),
    ]


_REBUILT = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "Checking."},
    {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q": "tea"}'},
    {"type": "function_call_output", "call_id": "call_1", "output": "found"},
    {"role": "assistant", "content": "Done."},
    {"role": "user", "content": "more"},
]


@pytest.mark.parametrize(
    ("raw", "wire"),
    [
        ([reasoning_item("rs_9", "ENC-OTHER-77")], "openai:gpt-4o"),
        ([reasoning_item("rs_9", "ENC-OTHER-77")], "azure:gpt-5.2"),
        ([{"type": "thinking", "thinking": "T", "signature": "ENC-OTHER-77"}], "anthropic:claude-haiku-5-5"),
        ([], ""),
    ],
    ids=["another-model", "another-provider", "anthropic-thinking", "a-row-saved-before-raw-existed"],
)
async def test_a_message_from_another_wire_or_without_raw_is_rebuilt_from_its_text_and_calls(
    openai_settings, raw, wire
):
    llm = ScriptedOpenAI(openai_settings, [[message_item("ok")]])
    await llm.provider.complete(_history(raw, wire))

    assert llm.paths == ["/v1/responses"]
    assert llm.requests[0]["input"] == _REBUILT
    assert "ENC-OTHER-77" not in json.dumps(llm.requests[0])


def _tool_message(raw: list[dict[str, Any]], call_id: str = "call_1", content: str = "Checking.") -> list[LLMMessage]:
    call = ToolCall(id=call_id, name="lookup", arguments={})
    return [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=[call], raw=raw, wire="openai:gpt-5.2"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="found", tool_call_id=call_id),
    ]


async def _sent_input(settings, history: list[LLMMessage]) -> list[dict[str, Any]]:
    llm = ScriptedOpenAI(settings, [[message_item("ok")]])
    await llm.provider.complete(history)
    assert llm.paths == ["/v1/responses"]
    return llm.requests[0]["input"]


async def test_raw_whose_calls_are_not_the_messages_calls_is_rebuilt(openai_settings):
    """Pair repair can drop a call after its response: the stored items would then answer a call that
    is no longer there, so the message is rebuilt from its tool calls."""
    stale = [reasoning_item("rs_1", "ENC-1"), function_call_item("lookup", {}, "call_1", "fc_1")]
    sent = await _sent_input(openai_settings, _tool_message(stale, call_id="call_2"))

    assert sent == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Checking."},
        {"type": "function_call", "call_id": "call_2", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_2", "output": "found"},
    ]


async def test_reasoning_with_nothing_after_it_is_never_sent(openai_settings):
    """A reply cut off while it was still reasoning has no message or call: a lone reasoning item has
    nothing to belong to, so it is not sent, and a reasoning item after the last real item is trimmed."""
    cut = [reasoning_item("rs_1", "ENC-1")]
    only = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", raw=cut, wire="openai:gpt-5.2"),
        LLMMessage(role=MessageRole.USER, content="again"),
    ]
    assert await _sent_input(openai_settings, only) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "again"},
    ]

    trailing = [reasoning_item("rs_1", "ENC-1"), message_item("Hi.", "msg_1"), reasoning_item("rs_2", "ENC-2")]
    history = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content="Hi.", raw=trailing, wire="openai:gpt-5.2"),
        LLMMessage(role=MessageRole.USER, content="again"),
    ]
    sent = await _sent_input(openai_settings, history)
    assert [item.get("id") for item in sent[1:3]] == ["rs_1", "msg_1"]
    assert len(sent) == 4  # user, the two items, user: rs_2 is gone


# ── User messages ────────────────────────────────────────────────────────────


async def test_a_user_message_sends_its_context_then_its_photos_then_its_words(openai_settings):
    photo = ImageContent.from_bytes(PNG_1X1, "image/png")
    history = [
        LLMMessage(role=MessageRole.USER, content="What is this?", images=[photo], context="It is 09:00."),
        LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nTea."),
    ]
    sent = await _sent_input(openai_settings, history)

    assert sent == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "It is 09:00."},
                {"type": "input_image", "image_url": f"data:image/png;base64,{photo.data}", "detail": "auto"},
                {"type": "input_text", "text": "What is this?"},
            ],
        },
        {"role": "system", "content": "[Previous conversation summary]\nTea."},
    ]


async def test_a_photo_from_the_first_turn_is_in_every_later_request_at_the_same_place(
    openai_settings, session_manager, cost_tracker
):
    await session_manager.onboarding.complete("user1", "test", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_SECOND, _THIRD, _SECOND])
    await agent.handle_message("user1", "test", "What is this?", images=[(PNG_1X1, "image/png")], memory=False)
    await agent.handle_message("user1", "test", "And now?", memory=False)
    await agent.handle_message("user1", "test", "Still there?", memory=False)

    assert llm.paths == ["/v1/responses"] * 3

    def photos(request: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        return [
            (position, part)
            for position, item in enumerate(request["input"])
            if isinstance(item.get("content"), list)
            for part in item["content"]
            if part["type"] == "input_image"
        ]

    first = photos(llm.requests[0])
    assert len(first) == 1  # precondition: the photo reached the first request
    assert photos(llm.requests[1]) == first
    assert photos(llm.requests[2]) == first


# ── Effort, temperature and `include` ────────────────────────────────────────


async def _body(settings, **call: Any) -> dict[str, Any]:
    llm = ScriptedOpenAI(settings, [[message_item("ok")]], model=settings.openai_model)
    await llm.provider.complete(_HI, **call)
    assert llm.paths == ["/v1/responses"]
    return llm.requests[0]


async def test_the_effort_setting_is_sent_as_reasoning_effort_and_a_reasoning_model_gets_no_temperature(
    openai_settings,
):
    body = await _body(openai_settings.model_copy(update={"reasoning_effort": "high"}))

    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body  # Chat Completions' spelling
    assert "temperature" not in body
    assert body["include"] == ["reasoning.encrypted_content"]
    # A value given for one call replaces the setting, as on Chat Completions
    assert (await _body(openai_settings, reasoning_effort="low"))["reasoning"] == {"effort": "low"}
    assert "reasoning" not in await _body(openai_settings)


async def test_a_model_that_does_not_reason_gets_its_temperature_and_no_include(openai_settings):
    """The docs do not say what `include` does for a model without reasoning items, so it is not sent."""
    body = await _body(openai_settings.model_copy(update={"openai_model": "gpt-4o", "temperature": 0.3}))

    assert body["temperature"] == 0.3
    assert "include" not in body
    assert "reasoning" not in body


async def test_gpt6_with_tools_and_no_effort_sends_no_reasoning_so_the_model_uses_its_default(openai_settings):
    """On Chat Completions gpt-6 takes tools only at `none`, so that is what a tool turn sent. The
    Responses API takes tools at any effort: nothing set means nothing sent (slice 8)."""
    settings = openai_settings.model_copy(update={"openai_model": "gpt-6.1-terra"})

    assert "reasoning" not in await _body(settings, tools=GOLDEN_TOOLS)


# ── Errors ───────────────────────────────────────────────────────────────────


async def test_a_rate_limit_is_retried_after_a_wait(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [429, [message_item("Back.")]])
    with patch("pincer.llm.openai_common.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses", "/v1/responses"]
    assert result.content == "Back."
    sleep.assert_awaited_once()


async def test_a_rate_limit_that_lasts_raises_after_the_attempts(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [429, 429, 429])
    with patch("pincer.llm.openai_common.asyncio.sleep", new=AsyncMock()), pytest.raises(LLMRateLimitError):
        await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"] * 3


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (400, False)])
async def test_an_error_status_names_the_provider_and_says_whether_to_ask_again(openai_settings, status, retryable):
    llm = ScriptedOpenAI(openai_settings, [status])
    with pytest.raises(LLMError, match=f"openai API error: {status}") as raised:
        await llm.provider.complete(_HI)

    assert llm.paths == ["/v1/responses"]
    assert raised.value.retryable is retryable


async def test_a_connection_error_names_the_provider(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [httpx.ConnectError("refused")])
    with pytest.raises(LLMError, match="openai connection error"):
        await llm.provider.complete(_HI)


# ── The Azure shape ──────────────────────────────────────────────────────────


async def test_the_stream_path_uses_the_responses_api_and_sends_the_reasoning_a_complete_turn_saved(
    openai_settings, session_manager, cost_tracker
):
    """Before slice 7 a streamed turn on a listed endpoint was a Chat Completions request that rebuilt the
    history without its reasoning. It is now a Responses request that sends the reasoning items."""
    await session_manager.onboarding.complete("user1", "test", now=1.0)
    agent, llm = _agent(openai_settings, session_manager, cost_tracker, [_FIRST, _SECOND, "Streamed."])
    await agent.handle_message("user1", "test", "Which tea?", memory=False)
    [c async for c in agent.handle_message_stream("user1", "test", "And coffee?", memory=False)]

    assert llm.paths == ["/v1/responses", "/v1/responses", "/v1/responses"]
    body = llm.requests[2]
    assert body["stream"] is True
    assert _kinds(body) == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "message",
        "user",
    ]
    assert body["input"][1:3] == _FIRST and body["input"][4:6] == _SECOND


async def test_an_azure_endpoint_posts_to_the_v1_responses_url_without_an_api_version(settings):
    azure = settings.model_copy(
        update={
            "openai_compatible_provider": "azure",
            "openai_compatible_base_url": "https://res.openai.azure.com/openai/v1/",
            "openai_compatible_api_key": SecretStr("azure-key"),
            "openai_compatible_model": "gpt-6.1-terra",  # the Azure deployment name
            "reasoning_effort": "",
            "thinking": "",
        }
    )
    llm = ScriptedOpenAI(azure, [[message_item("ok")]], provider="azure", model="gpt-6.1-terra")
    await llm.provider.complete(_HI)

    assert llm.urls == ["https://res.openai.azure.com/openai/v1/responses"]
    assert llm.headers[0]["authorization"] == "Bearer azure-key"
    assert llm.requests[0]["model"] == "gpt-6.1-terra"


async def test_the_cost_log_gets_the_split_usage_of_every_request(openai_settings, session_manager, cost_tracker):
    """What the agent records for a tool turn: `test-fixture-model` is priced in tests/fixtures/pricing."""
    await session_manager.onboarding.complete("user1", "test", now=1.0)
    settings = openai_settings.model_copy(update={"openai_model": "test-fixture-model"})
    replies: list[Reply] = [
        (_FIRST, usage(1000, 200, cached=600, reasoning=150)),
        (_SECOND, usage(1000, 200, cached=600, written=100)),
    ]
    registry = ToolRegistry()

    async def lookup(q: str = "") -> str:
        return f"found {q}"

    registry.register(name="lookup", description="Look something up", handler=lookup)
    llm = ScriptedOpenAI(settings, replies, model="test-fixture-model")
    await Agent(settings, llm.provider, session_manager, cost_tracker, registry).handle_message(
        "user1", "test", "Which tea?", memory=False
    )

    assert llm.paths == ["/v1/responses"] * 2
    async with session_scope(get_database_url(cost_tracker._db_path)) as session:
        rows = (await session.exec(select(CostLog).order_by(CostLog.timestamp))).all()
    assert [
        (
            r.provider,
            r.input_tokens,
            r.cache_read_tokens,
            r.cache_write_5m_tokens,
            r.cache_write_1h_tokens,
            r.output_tokens,
        )
        for r in rows
    ] == [("openai", 400, 600, 0, 0, 200), ("openai", 300, 600, 100, 0, 200)]
