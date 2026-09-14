"""Tests for AnthropicCompatibleProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from pincer.llm.base import ImageContent, LLMMessage, MessageRole, ToolCall

MODULE = "pincer.llm.anthropic_common"


def _make_message(text: str = "Hi!", model: str = "claude-x") -> Message:
    return Message.model_construct(
        content=[TextBlock.model_construct(type="text", text=text)],
        model=model,
        usage=Usage.model_construct(input_tokens=10, output_tokens=5),
        stop_reason="end_turn",
    )


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
        mock_client.messages.create = AsyncMock(return_value=_make_message("Hello!"))
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
        mock_client.messages.create = AsyncMock(return_value=msg)
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
        mock_client.messages.create = AsyncMock(return_value=_make_message())
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
        await provider.complete(
            [LLMMessage(role=MessageRole.USER, content="hi")],
            tools=tools,
            system="be nice",
        )

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["system"] == "be nice"
    # tools pass through with the Sprint-5 cache breakpoint on the last one
    sent = kwargs["tools"]
    assert [{k: v for k, v in t_.items() if k != "cache_control"} for t_ in sent] == tools
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_complete_wraps_status_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    err = anthropic.APIStatusError("bad", response=MagicMock(status_code=400), body={})

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=err)
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
        mock_client.messages.create = AsyncMock(return_value=_make_message())
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

    sent = mock_client.messages.create.call_args.kwargs["messages"]
    assert sent[0]["role"] == "user"  # first message must be user


@pytest.mark.asyncio
async def test_stream_yields_text(anthropic_settings):
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    async def _text_stream():
        for t in ["He", "llo"]:
            yield t

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=MagicMock(text_stream=_text_stream()))
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=stream_cm)
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        result = [t async for t in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")])]

    assert result == ["He", "llo"]


@pytest.mark.asyncio
async def test_complete_retries_then_succeeds(anthropic_settings):
    import anthropic

    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    rate_err = anthropic.RateLimitError("slow down", response=MagicMock(headers={"retry-after": "0"}), body={})
    create = AsyncMock(side_effect=[rate_err, _make_message("after retry")])

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls, patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.messages.create = create
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "after retry"
    assert create.call_count == 2


@pytest.mark.asyncio
async def test_complete_rate_limit_exhausted(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMRateLimitError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    rate_err = anthropic.RateLimitError("slow down", response=MagicMock(headers={"retry-after": "0"}), body={})

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls, patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=rate_err)
        mock_cls.return_value = mock_client

        provider = AnthropicCompatibleProvider(anthropic_settings, "anthropic")
        with pytest.raises(LLMRateLimitError):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.mark.asyncio
async def test_stream_rate_limit_error(anthropic_settings):
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
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_stream_status_error(anthropic_settings):
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
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_complete_connection_error(anthropic_settings):
    import anthropic

    from pincer.exceptions import LLMError
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    err = anthropic.APIConnectionError(request=MagicMock())

    with patch(f"{MODULE}.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=err)
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
        mock_client.messages.create = AsyncMock(return_value=_make_message())
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

    sent = mock_client.messages.create.call_args.kwargs["messages"]
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


# ── Prompt caching on tool schemas (Sprint 5, T5.4) ──────────────────


class TestToolPromptCaching:
    def _provider(self, settings):
        from pincer.llm.anthropic_common import AnthropicCompatibleProvider

        with patch(f"{MODULE}.AsyncAnthropic"):
            return AnthropicCompatibleProvider(settings, "anthropic")

    def test_last_tool_carries_cache_control(self, anthropic_settings):
        provider = self._provider(anthropic_settings)
        tools = [
            {"name": "a", "description": "x", "input_schema": {}},
            {"name": "b", "description": "y", "input_schema": {}},
        ]
        cached = provider._maybe_cache_tools(tools)
        assert "cache_control" not in cached[0]
        assert cached[-1]["cache_control"] == {"type": "ephemeral"}
        # the caller's list and dicts are never mutated
        assert "cache_control" not in tools[-1]

    def test_disabled_via_setting(self, anthropic_settings):
        anthropic_settings.prompt_cache_tools = False
        provider = self._provider(anthropic_settings)
        tools = [{"name": "a", "description": "x", "input_schema": {}}]
        assert provider._maybe_cache_tools(tools) is tools

    async def test_complete_sends_cached_tools(self, anthropic_settings):
        provider = self._provider(anthropic_settings)
        provider._client = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=_make_message())
        tools = [{"name": "a", "description": "x", "input_schema": {}}]

        await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")], tools=tools)

        sent_tools = provider._client.messages.create.call_args.kwargs["tools"]
        assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}
