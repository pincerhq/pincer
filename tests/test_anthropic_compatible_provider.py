"""Tests for AnthropicCompatibleProvider."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic_sse as sse
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from pincer.config.pricing import TokenUsage
from pincer.llm.base import ImageContent, LLMMessage, MessageRole, ThinkingBlock, ToolCall

MODULE = "pincer.llm.anthropic_common"


def _make_message(text: str = "Hi!", model: str = "claude-x") -> Message:
    return Message.model_construct(
        content=[TextBlock.model_construct(type="text", text=text)],
        model=model,
        usage=Usage.model_construct(input_tokens=10, output_tokens=5),
        stop_reason="end_turn",
    )


def _final_stream(message: Message) -> MagicMock:
    """A `messages.stream()` context manager whose final message is `message`."""
    stream = MagicMock()
    stream.get_final_message = AsyncMock(return_value=message)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _failing_stream(err: Exception) -> MagicMock:
    """A `messages.stream()` context manager that fails when the request is sent."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=err)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture
def anthropic_settings():
    from pincer.config import Settings

    return Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="claude-x",
    )


def test_well_known_anthropic_uses_default_endpoint(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        AnthropicCompatibleProvider(anthropic_settings, "anthropic")

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["api_key"] == "sk-ant-test"
    assert kwargs["base_url"] is None


def test_compatible_uses_custom_base_url_and_key():
    from pincer.config import Settings
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="my-claude",
        default_model="claude-x",
        anthropic_compatible_provider="my-claude",
        anthropic_compatible_base_url="https://proxy.example/v1",
        anthropic_compatible_api_key="proxy-key",  # type: ignore[arg-type]
    )

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        AnthropicCompatibleProvider(settings, "my-claude")

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["base_url"] == "https://proxy.example/v1"
    assert kwargs["api_key"] == "proxy-key"


def test_compatible_empty_key_uses_placeholder():
    from pincer.config import Settings
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="proxy",
        anthropic_compatible_provider="proxy",
        anthropic_compatible_base_url="https://proxy.example/v1",
    )

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        AnthropicCompatibleProvider(settings, "proxy")

    assert mock_cls.call_args.kwargs["api_key"] == "none"


def test_anthropic_model_override():
    from pincer.config import Settings
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="claude-sonnet-4-5-20250929",
        anthropic_model="claude-opus-4-6",
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = AnthropicCompatibleProvider(settings, "anthropic")
    assert provider._default_model == "claude-opus-4-6"


def test_anthropic_compatible_model_falls_back_to_default():
    from pincer.config import Settings
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="my-claude",
        default_model="claude-sonnet-4-5-20250929",
        anthropic_compatible_provider="my-claude",
        anthropic_compatible_base_url="https://proxy/v1",
        # anthropic_compatible_model unset → falls back to default_model
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = AnthropicCompatibleProvider(settings, "my-claude")
    assert provider._default_model == "claude-sonnet-4-5-20250929"


@pytest.mark.asyncio
async def test_complete_returns_content(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_final_stream(_make_message("Hello!")))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "Hello!"
    assert result.model == "claude-x"
    assert result.provider == "anthropic"  # stamps the serving provider
    assert result.input_tokens == 10
    assert result.output_tokens == 5


def test_missing_anthropic_key_raises():
    from pincer.config import Settings
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = Settings(
        anthropic_api_key="",  # type: ignore[arg-type]
        openai_api_key="sk-oai",  # type: ignore[arg-type]  (some provider must be set)
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncAnthropic"), pytest.raises(ValueError, match="PINCER_ANTHROPIC_API_KEY"):
        AnthropicCompatibleProvider(settings, "anthropic")


@pytest.mark.asyncio
async def test_complete_parses_tool_use(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    tool_block = ToolUseBlock.model_construct(type="tool_use", id="t1", name="search", input={"q": "x"})
    msg = Message.model_construct(
        content=[tool_block],
        model="claude-x",
        usage=Usage.model_construct(input_tokens=3, output_tokens=2),
        stop_reason="tool_use",
    )

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_final_stream(msg))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.has_tool_calls
    assert result.tool_calls[0].name == "search"
    assert result.tool_calls[0].arguments == {"q": "x"}


@pytest.mark.asyncio
async def test_complete_passes_system_and_tools(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_final_stream(_make_message()))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
        await provider.complete(
            [LLMMessage(role=MessageRole.USER, content="hi")],
            tools=tools,
            system="be nice",
        )

    kwargs = mock_client.messages.stream.call_args.kwargs
    # tools and system pass through with their 1-hour cache breakpoints (issue #357 slice 10)
    assert kwargs["system"] == [
        {"type": "text", "text": "be nice", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    sent = kwargs["tools"]
    assert [{k: v for k, v in t_.items() if k != "cache_control"} for t_ in sent] == tools
    assert sent[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


@pytest.mark.asyncio
async def test_complete_wraps_status_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    err = anthropic.APIStatusError("bad", response=MagicMock(status_code=400), body={})

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_failing_stream(err))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMError, match="Anthropic API error"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.mark.asyncio
async def test_convert_handles_images_tool_results(anthropic_settings):
    """Exercise the message-conversion branches (images, tool_use, tool_result)."""
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_final_stream(_make_message()))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        messages = [
            LLMMessage(role=MessageRole.SYSTEM, content="ignored"),
            LLMMessage(
                role=MessageRole.USER,
                content="look",
                images=[ImageContent(data="abc", media_type="image/png")],
            ),
            LLMMessage(
                role=MessageRole.ASSISTANT,
                content="calling",
                tool_calls=[ToolCall(id="t1", name="search", arguments={"q": "x"})],
            ),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="result", tool_call_id="t1"),
        ]
        await provider.complete(messages)

    sent = mock_client.messages.stream.call_args.kwargs["messages"]
    assert sent[0]["role"] == "user"  # first message must be user


@pytest.mark.asyncio
async def test_complete_retries_then_succeeds(anthropic_settings):
    import anthropic

    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    rate_err = anthropic.RateLimitError("slow down", response=MagicMock(headers={"retry-after": "0"}), body={})
    create = MagicMock(side_effect=[_failing_stream(rate_err), _final_stream(_make_message("after retry"))])

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls, patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.messages.stream = create
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "after retry"
    assert create.call_count == 2


@pytest.mark.asyncio
async def test_complete_accepts_max_tokens_the_sdk_refuses_to_send_unstreamed(anthropic_settings):
    """PINCER_MAX_TOKENS=32768: the real SDK raises "Streaming is required for
    operations that may take longer than 10 minutes" on a non-streaming call."""
    import json

    import httpx
    from anthropic import AsyncAnthropic

    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-x",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
    await provider._client.close()
    provider._client = AsyncAnthropic(
        api_key="sk-ant-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")], max_tokens=32768)
    await provider.close()

    assert result.content == "Hello!"
    assert result.stop_reason == "end_turn"
    assert sent[0]["max_tokens"] == 32768
    assert sent[0]["stream"] is True


@pytest.mark.asyncio
async def test_complete_rate_limit_exhausted(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMRateLimitError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    rate_err = anthropic.RateLimitError("slow down", response=MagicMock(headers={"retry-after": "0"}), body={})

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls, patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_failing_stream(rate_err))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMRateLimitError):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.mark.asyncio
async def test_stream_turn_rate_limit_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMRateLimitError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    rate_err = anthropic.RateLimitError("slow down", response=MagicMock(headers={"retry-after": "1"}), body={})
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=rate_err)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=stream_cm)
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMRateLimitError):
            async for _ in provider.stream_turn([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_stream_turn_status_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    err = anthropic.APIStatusError("server", response=MagicMock(status_code=500), body={})
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=err)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=stream_cm)
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMError, match="stream error"):
            async for _ in provider.stream_turn([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_complete_connection_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    err = anthropic.APIConnectionError(request=MagicMock())

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_failing_stream(err))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMError, match="connection error"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.mark.asyncio
async def test_validate_merges_and_drops_orphans(anthropic_settings):
    """Consecutive same-role messages merge; orphaned tool_use is stripped."""
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_final_stream(_make_message()))
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        messages = [
            LLMMessage(role=MessageRole.USER, content="one"),
            LLMMessage(role=MessageRole.USER, content="two"),
            # orphaned tool_use (no matching tool_result) → dropped
            LLMMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="orphan", name="t", arguments={})],
            ),
        ]
        await provider.complete(messages)

    sent = mock_client.messages.stream.call_args.kwargs["messages"]
    # the two consecutive user messages were merged into one
    assert sent[0]["role"] == "user"
    assert "one" in sent[0]["content"] and "two" in sent[0]["content"]


@pytest.mark.asyncio
async def test_close(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        await provider.close()

    mock_client.close.assert_awaited_once()


# ── Per-model request params: temperature only where the model accepts it ──


class TestTemperatureByModel:
    REJECTS = "claude-haiku-5-5"
    ACCEPTS = "claude-haiku-4-5-20251001"

    def _provider(self, settings):
        from pincer.llm.anthropic_common import AnthropicCompatibleProvider

        with patch(f"{MODULE}.AsyncAnthropic"):
            provider = AnthropicCompatibleProvider(settings, "anthropic")

        async def _text_stream():
            yield "ok"

        stream = MagicMock(text_stream=_text_stream())
        stream.get_final_message = AsyncMock(return_value=_make_message())
        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=stream)
        stream_cm.__aexit__ = AsyncMock(return_value=False)

        provider._client = MagicMock()
        provider._client.messages.stream = MagicMock(return_value=stream_cm)
        return provider

    async def _sent_kwargs(self, provider, method, **call_kwargs):
        messages = [LLMMessage(role=MessageRole.USER, content="hi")]
        if method == "complete":
            await provider.complete(messages, **call_kwargs)
            return provider._client.messages.stream.call_args.kwargs
        _ = [e async for e in getattr(provider, method)(messages, **call_kwargs)]
        return provider._client.messages.stream.call_args.kwargs

    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_dropped_for_model_that_rejects_it(self, anthropic_settings, method):
        provider = self._provider(anthropic_settings)
        kwargs = await self._sent_kwargs(provider, method, model=self.REJECTS, temperature=0, max_tokens=150)
        assert "temperature" not in kwargs
        assert kwargs["model"] == self.REJECTS
        assert kwargs["max_tokens"] == 150

    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_sent_for_model_that_accepts_it(self, anthropic_settings, method):
        provider = self._provider(anthropic_settings)
        kwargs = await self._sent_kwargs(provider, method, model=self.ACCEPTS, temperature=0)
        assert kwargs["temperature"] == 0  # an explicit 0 is not replaced by the default

    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_default_model_and_temperature(self, anthropic_settings, method):
        """No per-call model: the rule applies to the provider's default model."""
        anthropic_settings.anthropic_model = self.REJECTS
        provider = self._provider(anthropic_settings)
        assert "temperature" not in await self._sent_kwargs(provider, method)

        anthropic_settings.anthropic_model = self.ACCEPTS
        provider = self._provider(anthropic_settings)
        kwargs = await self._sent_kwargs(provider, method)
        assert kwargs["temperature"] == anthropic_settings.temperature


# ── Thinking blocks (issue #345) ─────────────────────

_THOUGHT = "SECRET-THOUGHT-7f3a"
_SIGNATURE = "SIG-7f3a-signature"
_REDACTED = "REDACTED-7f3a-data"
_THINKING_BLOCKS = [
    ThinkingBlock("thinking", thinking=_THOUGHT, signature=_SIGNATURE),
    ThinkingBlock("redacted_thinking", data=_REDACTED),
]
_THINKING_API = [
    {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
    {"type": "redacted_thinking", "data": _REDACTED},
]
_THINKING_REPLY = (
    [sse.thinking(_THOUGHT, _SIGNATURE), sse.redacted(_REDACTED), sse.text("Hello!"), sse.tool_use("toolu_1", "t", {})],
    "tool_use",
)


async def _sent_messages(settings, messages: list[LLMMessage]) -> list[dict]:
    """The `messages` of the request the provider really sends for this history."""
    async with sse.ScriptedAnthropic(settings, [([sse.text("ok")], "end_turn")]) as llm:
        await llm.provider.complete(messages)
    return llm.requests[0]["messages"]


async def test_complete_keeps_thinking_blocks_verbatim_in_order(anthropic_settings):
    async with sse.ScriptedAnthropic(anthropic_settings, [_THINKING_REPLY]) as llm:
        result = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.thinking_blocks == _THINKING_BLOCKS
    assert result.content == "Hello!"
    assert [tc.id for tc in result.tool_calls] == ["toolu_1"]


async def test_stream_turn_keeps_thinking_blocks_and_never_streams_them_as_text(anthropic_settings):
    async with sse.ScriptedAnthropic(anthropic_settings, [_THINKING_REPLY]) as llm:
        events = [e async for e in llm.provider.stream_turn([LLMMessage(role=MessageRole.USER, content="hi")])]

    assert "".join(e.text for e in events) == "Hello!"
    final = events[-1].response
    assert final is not None
    assert final.thinking_blocks == _THINKING_BLOCKS
    assert final.content == "Hello!"


async def test_tool_call_turn_sends_thinking_first_then_text_then_tool_use(anthropic_settings):
    call = ToolCall(id="toolu_1", name="t", arguments={"a": 1})
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(
                role=MessageRole.ASSISTANT,
                content="Checking.",
                tool_calls=[call],
                thinking_blocks=list(_THINKING_BLOCKS),
            ),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="42", tool_call_id="toolu_1"),
        ],
    )

    assert sent[1] == {
        "role": "assistant",
        "content": [
            *_THINKING_API,
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {"a": 1}},
        ],
    }


async def test_text_turn_sends_thinking_first_then_text(anthropic_settings):
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(role=MessageRole.ASSISTANT, content="Hello!", thinking_blocks=list(_THINKING_BLOCKS)),
            LLMMessage(role=MessageRole.USER, content="again"),
        ],
    )

    assert sent[1] == {"role": "assistant", "content": [*_THINKING_API, {"type": "text", "text": "Hello!"}]}


async def test_text_turn_without_text_never_sends_thinking_alone(anthropic_settings):
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(role=MessageRole.ASSISTANT, content="", thinking_blocks=list(_THINKING_BLOCKS)),
            LLMMessage(role=MessageRole.USER, content="again"),
        ],
    )

    assert sent[1] == {"role": "assistant", "content": ""}  # exactly as without thinking


async def test_merging_assistant_turns_keeps_each_messages_blocks_in_order(anthropic_settings):
    call = ToolCall(id="toolu_1", name="t", arguments={})
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(role=MessageRole.ASSISTANT, content="One moment."),
            LLMMessage(
                role=MessageRole.ASSISTANT, content="", tool_calls=[call], thinking_blocks=list(_THINKING_BLOCKS)
            ),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="42", tool_call_id="toolu_1"),
        ],
    )

    assert sent[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "One moment."},
            *_THINKING_API,
            {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}},
        ],
    }


async def test_orphaned_tool_call_turn_is_dropped_with_its_thinking(anthropic_settings):
    orphan = ToolCall(id="orphan", name="t", arguments={})
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(
                role=MessageRole.ASSISTANT, content="", tool_calls=[orphan], thinking_blocks=list(_THINKING_BLOCKS)
            ),
            LLMMessage(role=MessageRole.USER, content="again"),
        ],
    )

    assert sent == [{"role": "user", "content": "hi\nagain"}]


# ── Effort and thinking settings (issue #345) ─────────────────────

_HI = [LLMMessage(role=MessageRole.USER, content="hi")]
_OK_REPLY = ([sse.text("ok")], "end_turn")


async def _sent_request(settings, method: str = "complete", **call_kwargs) -> dict:
    """The request body the provider really sends for one call of `method`."""
    async with sse.ScriptedAnthropic(settings, [_OK_REPLY]) as llm:
        if method == "complete":
            await llm.provider.complete(_HI, **call_kwargs)
        else:
            _ = [e async for e in getattr(llm.provider, method)(_HI, **call_kwargs)]
    assert len(llm.requests) == 1
    return llm.requests[0]


class TestReasoningSettings:
    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_global_effort_and_thinking_reach_the_request(self, anthropic_settings, method):
        settings = anthropic_settings.model_copy(
            update={"default_model": "claude-haiku-5-5", "reasoning_effort": "low", "thinking": "adaptive"}
        )
        body = await _sent_request(settings, method)

        assert body["model"] == "claude-haiku-5-5"
        assert body["output_config"] == {"effort": "low"}
        assert body["thinking"] == {"type": "adaptive"}

    @pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-sonnet-4-6"])
    async def test_adaptive_thinking_drops_temperature(self, anthropic_settings, model):
        """These models still take `temperature`, but not together with thinking (a 400)."""
        settings = anthropic_settings.model_copy(update={"default_model": model, "thinking": "adaptive"})
        body = await _sent_request(settings, temperature=0)

        assert body["thinking"] == {"type": "adaptive"}
        assert "temperature" not in body

    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-opus-5-1",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-haiku-6",
            "claude-opus-5-5",
            "claude-fable-5-1",
            "claude-mythos-5-1",
            "claude-3-5-sonnet-20241022",
        ],
    )
    async def test_empty_settings_send_what_they_sent_before(self, anthropic_settings, model):
        settings = anthropic_settings.model_copy(update={"default_model": model})
        assert (settings.reasoning_effort, settings.thinking) == ("", "")
        body = await _sent_request(settings)

        takes_temperature = model in ("claude-haiku-4-5-20251001", "claude-sonnet-4-5", "claude-opus-4-5")
        takes_temperature |= model in ("claude-opus-4-6", "claude-sonnet-4-6", "claude-3-5-sonnet-20241022")
        expected = {"model", "max_tokens", "messages", "stream"} | ({"temperature"} if takes_temperature else set())
        assert set(body) == expected
        if takes_temperature:
            assert body["temperature"] == settings.temperature

    @staticmethod
    async def _two_requests(settings) -> None:
        async with sse.ScriptedAnthropic(settings, [_OK_REPLY, _OK_REPLY]) as llm:
            await llm.provider.complete(_HI)
            await llm.provider.complete(_HI)
        assert len(llm.requests) == 2

    @pytest.mark.parametrize(
        ("model", "update", "setting"),
        [
            # unrecognised: a deployment name that hides the Claude model
            ("voice-fast-warn-once", {"reasoning_effort": "low"}, "reasoning_effort=low"),
            # recognised, but the model rejects the setting
            ("us.anthropic.claude-haiku-4-5-warn-once", {"thinking": "adaptive"}, "thinking=adaptive"),
        ],
    )
    async def test_a_dropped_setting_warns_once(self, anthropic_settings, caplog, model, update, setting):
        settings = anthropic_settings.model_copy(update={"default_model": model, **update})
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            await self._two_requests(settings)

        lines = [r for r in caplog.records if model in r.getMessage() and setting in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.WARNING, logging.DEBUG]

    async def test_empty_settings_never_warn_for_an_unrecognised_model(self, anthropic_settings, caplog):
        settings = anthropic_settings.model_copy(update={"default_model": "voice-fast-benign"})
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            await self._two_requests(settings)

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_call_arguments_replace_the_global_settings(self, anthropic_settings, method):
        settings = anthropic_settings.model_copy(
            update={"default_model": "claude-haiku-5-5", "reasoning_effort": "high", "thinking": "adaptive"}
        )
        body = await _sent_request(settings, method, reasoning_effort="low", thinking="disabled")

        assert body["output_config"] == {"effort": "low"}
        assert body["thinking"] == {"type": "disabled"}

    @pytest.mark.parametrize("method", ["complete", "stream_turn"])
    async def test_none_arguments_keep_the_global_settings(self, anthropic_settings, method):
        settings = anthropic_settings.model_copy(
            update={"default_model": "claude-haiku-5-5", "reasoning_effort": "high", "thinking": "adaptive"}
        )
        body = await _sent_request(settings, method, reasoning_effort=None, thinking=None)

        assert body["output_config"] == {"effort": "high"}
        assert body["thinking"] == {"type": "adaptive"}

    async def test_empty_arguments_send_nothing_even_when_global_settings_are_set(self, anthropic_settings):
        """`""` means "send nothing" for this call; only None falls back to the global setting."""
        settings = anthropic_settings.model_copy(
            update={"default_model": "claude-haiku-5-5", "reasoning_effort": "high", "thinking": "adaptive"}
        )
        body = await _sent_request(settings, reasoning_effort="", thinking="")

        assert body["model"] == "claude-haiku-5-5"
        assert "output_config" not in body
        assert "thinking" not in body


# ── Block order within a response (issue #345, review round 1) ─────────────────

_SECOND_THOUGHT = "SECOND-THOUGHT-9c1d"
_SECOND_SIGNATURE = "SIG-9c1d-signature"
_INTERLEAVED_REPLY = (
    [
        sse.thinking(_THOUGHT, _SIGNATURE),
        sse.text("Checking."),
        sse.thinking(_SECOND_THOUGHT, _SECOND_SIGNATURE),
        sse.tool_use("toolu_1", "t", {"a": 1}),
    ],
    "tool_use",
)
_INTERLEAVED_API = [
    {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
    {"type": "text", "text": "Checking."},
    {"type": "thinking", "thinking": _SECOND_THOUGHT, "signature": _SECOND_SIGNATURE},
    {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {"a": 1}},
]


def _saved_as_the_agent_does(result) -> LLMMessage:
    return LLMMessage(
        role=MessageRole.ASSISTANT,
        content=result.content,
        tool_calls=result.tool_calls,
        thinking_blocks=list(result.thinking_blocks),
        content_blocks=list(result.content_blocks),
    )


async def test_an_interleaved_response_is_replayed_in_the_order_it_was_generated(anthropic_settings):
    async with sse.ScriptedAnthropic(anthropic_settings, [_INTERLEAVED_REPLY]) as llm:
        result = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            _saved_as_the_agent_does(result),
            LLMMessage(role=MessageRole.TOOL_RESULT, content="42", tool_call_id="toolu_1"),
        ],
    )

    assert sent[1] == {"role": "assistant", "content": _INTERLEAVED_API}


async def test_a_response_without_thinking_records_no_content_blocks(anthropic_settings):
    async with sse.ScriptedAnthropic(anthropic_settings, [([sse.text("Hello!")], "end_turn")]) as llm:
        result = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content_blocks == []


async def test_a_text_reply_with_thinking_in_the_middle_is_replayed_in_order(anthropic_settings):
    reply = ([sse.text("One."), sse.thinking(_THOUGHT, _SIGNATURE), sse.text("Two.")], "end_turn")
    async with sse.ScriptedAnthropic(anthropic_settings, [reply]) as llm:
        result = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            _saved_as_the_agent_does(result),
            LLMMessage(role=MessageRole.USER, content="again"),
        ],
    )

    assert sent[1]["content"] == [
        {"type": "text", "text": "One."},
        {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE},
        {"type": "text", "text": "Two."},
    ]


async def test_a_message_edited_after_the_response_falls_back_to_thinking_first(anthropic_settings):
    """Pair repair may drop a tool call from a saved message: the recorded order no longer fits it."""
    async with sse.ScriptedAnthropic(anthropic_settings, [_INTERLEAVED_REPLY]) as llm:
        result = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])
    edited = _saved_as_the_agent_does(result)
    edited.content = "Edited."
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            edited,
            LLMMessage(role=MessageRole.TOOL_RESULT, content="42", tool_call_id="toolu_1"),
        ],
    )

    assert [b["type"] for b in sent[1]["content"]] == ["thinking", "thinking", "text", "tool_use"]


async def test_merging_assistant_messages_never_moves_a_later_thinking_ahead_of_an_earlier_text(anthropic_settings):
    sent = await _sent_messages(
        anthropic_settings,
        [
            LLMMessage(role=MessageRole.USER, content="hi"),
            LLMMessage(role=MessageRole.ASSISTANT, content="One moment."),
            LLMMessage(role=MessageRole.ASSISTANT, content="Done.", thinking_blocks=list(_THINKING_BLOCKS)),
            LLMMessage(role=MessageRole.USER, content="again"),
        ],
    )

    assert sent[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "One moment."}, *_THINKING_API, {"type": "text", "text": "Done."}],
    }


# ── Usage: every token type (issue #357) ──────────────────────────

_CACHED_USAGE = {
    "input_tokens": 36,
    "cache_creation_input_tokens": 27_659,
    "cache_creation": {"ephemeral_5m_input_tokens": 27_559, "ephemeral_1h_input_tokens": 100},
    "cache_read_input_tokens": 281_470,
}


async def _usage_of(settings, method: str, usage: dict) -> TokenUsage:
    """The usage one scripted response carrying `usage` parses to, via `method`."""
    async with sse.ScriptedAnthropic(settings, [([sse.text("ok")], "end_turn", usage)]) as llm:
        if method == "complete":
            response = await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])
        else:
            events = [e async for e in llm.provider.stream_turn([LLMMessage(role=MessageRole.USER, content="hi")])]
            response = events[-1].response
    return response.usage


@pytest.mark.parametrize("method", ["complete", "stream_turn"])
async def test_both_paths_split_cache_writes_by_ttl(anthropic_settings, method):
    expected = TokenUsage(
        input_tokens=36,
        cache_write_5m_tokens=27_559,
        cache_write_1h_tokens=100,
        cache_read_tokens=281_470,
        output_tokens=5,  # the scripted stream's message_delta
    )
    assert await _usage_of(anthropic_settings, method, _CACHED_USAGE) == expected


async def test_writes_without_a_ttl_breakdown_are_five_minute_writes(anthropic_settings):
    """The default TTL: an endpoint that reports only the total wrote 5-minute entries."""
    usage = {"input_tokens": 36, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0}
    assert await _usage_of(anthropic_settings, "complete", usage) == TokenUsage(
        input_tokens=36, cache_write_5m_tokens=500, output_tokens=5
    )
