"""Tests for OpenAICompatibleProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import openai as _openai
import pytest

from pincer.llm.base import LLMMessage, MessageRole

MODULE = "pincer.llm._openai_common"


def _make_chat_completion(content: str = "Hello!", model: str = "test-model"):
    message = MagicMock()
    message.content = content
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5

    response = MagicMock()
    response.choices = [choice]
    response.model = model
    response.usage = usage
    return response


def _make_stream_chunk(content: str | None):
    delta = MagicMock()
    delta.content = content
    choice = MagicMock()
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


@pytest.fixture
def openai_settings():
    from pincer.config import Settings

    return Settings(
        openai_api_key="sk-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",  # type: ignore[arg-type]
        default_model="my-model",
    )


@pytest.mark.asyncio
async def test_complete_returns_content(openai_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    fake_response = _make_chat_completion("Hi!")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "Hi!"
    assert result.input_tokens == 10
    assert result.output_tokens == 5


@pytest.mark.asyncio
async def test_model_map_remaps_model_name(openai_settings):
    from pincer.config import Settings
    from pincer.llm._openai_common import OpenAICompatibleProvider

    settings = Settings(
        openai_api_key="sk-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",  # type: ignore[arg-type]
        default_model="claude-sonnet-4-5-20250929",
    )
    fake_response = _make_chat_completion(model="gpt-4o")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(settings)
        await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_connection_error_includes_provider_name(openai_settings):
    import openai

    from pincer.exceptions import LLMError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=openai.APIConnectionError(request=MagicMock()))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        with pytest.raises(LLMError, match="OpenAI connection error"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.fixture
def grok_settings():
    from pincer.config import Settings

    return Settings(
        grok_api_key="grok-test-key",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="grok",  # type: ignore[arg-type]
        default_model="grok-3",
    )


@pytest.mark.asyncio
async def test_grok_provider_initialises(grok_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = OpenAICompatibleProvider(grok_settings)

    assert provider._provider_name == "Grok"


def test_unsupported_provider_raises():
    from pincer.config.main import Settings
    from pincer.llm._openai_common import OpenAICompatibleProvider

    settings = Settings(
        openai_api_key="sk-x",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",  # type: ignore[arg-type]
    )
    object.__setattr__(settings, "default_provider", type("P", (), {"value": "unsupported"})())
    with patch(f"{MODULE}.AsyncOpenAI"), pytest.raises(ValueError, match="Unsupported"):
        OpenAICompatibleProvider(settings)


@pytest.mark.asyncio
async def test_complete_api_status_error(openai_settings):
    from pincer.exceptions import LLMError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=_openai.APIStatusError("bad request", response=MagicMock(status_code=400), body={})
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        with pytest.raises(LLMError, match="400"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


def _make_async_iterable(chunks):
    """Return a coroutine that when awaited yields chunks as an async iterable."""

    async def _gen():
        for c in chunks:
            yield c

    async def _create(**kwargs):
        return _gen()

    return _create


@pytest.mark.asyncio
async def test_stream_yields_content(openai_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    chunks = [_make_stream_chunk("Hello"), _make_stream_chunk(" world"), _make_stream_chunk(None)]

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _make_async_iterable(chunks)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        result = [t async for t in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")])]

    assert result == ["Hello", " world"]


@pytest.mark.asyncio
async def test_stream_with_tools(openai_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    chunks = [_make_stream_chunk("ok")]

    async def _gen():
        for c in chunks:
            yield c

    async def _create_with_tools_check(**kwargs):
        assert "tools" in kwargs
        return _gen()

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _create_with_tools_check
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        tools = [{"name": "my_tool", "description": "a tool", "input_schema": {"type": "object", "properties": {}}}]
        result = [t async for t in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")], tools=tools)]

    assert result == ["ok"]


@pytest.mark.asyncio
async def test_stream_rate_limit_error(openai_settings):
    from pincer.exceptions import LLMRateLimitError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    async def _raise(**kwargs):
        raise _openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body={})

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _raise
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        with pytest.raises(LLMRateLimitError):
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_stream_api_status_error(openai_settings):
    from pincer.exceptions import LLMError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    async def _raise(**kwargs):
        raise _openai.APIStatusError("server error", response=MagicMock(status_code=500), body={})

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _raise
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        with pytest.raises(LLMError, match="500"):
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_retry_on_rate_limit_exhausted(openai_settings):
    from pincer.exceptions import LLMRateLimitError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    call_count = 0

    async def _always_rate_limit(**kwargs):
        nonlocal call_count
        call_count += 1
        raise _openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body={})

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls, patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.chat.completions.create = _always_rate_limit
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings)
        with pytest.raises(LLMRateLimitError):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert call_count == 3  # max_retries=3


def test_build_llm_returns_openai_compatible_for_openai():
    from pincer.api._deps import _build_llm
    from pincer.config import Settings
    from pincer.llm._openai_common import OpenAICompatibleProvider

    settings = Settings(
        openai_api_key="sk-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",  # type: ignore[arg-type]
    )
    with patch(f"{MODULE}.AsyncOpenAI"):
        provider = _build_llm(settings)

    assert isinstance(provider, OpenAICompatibleProvider)
