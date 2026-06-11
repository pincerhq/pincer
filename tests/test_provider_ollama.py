"""Tests for OpenAICompatibleProvider configured with Ollama."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.llm.base import LLMMessage, MessageRole


def _make_chat_completion(content: str = "Hello!", model: str = "llama3.2"):
    """Build a minimal fake openai ChatCompletion object."""
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


@pytest.fixture
def ollama_settings():
    from pincer.config import Settings

    return Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="ollama",
        default_model="llama3.2",
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
    )


@pytest.mark.asyncio
async def test_complete_returns_content(ollama_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    fake_response = _make_chat_completion("Hi from Ollama!")

    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(ollama_settings, "ollama")
        messages = [LLMMessage(role=MessageRole.USER, content="Say hi")]
        response = await provider.complete(messages)

    assert response.content == "Hi from Ollama!"
    assert response.model == "llama3.2"
    assert response.input_tokens == 10
    assert response.output_tokens == 5


def _make_stream_chunk(content: str | None):
    delta = MagicMock()
    delta.content = content

    choice = MagicMock()
    choice.delta = delta

    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


@pytest.mark.asyncio
async def test_stream_yields_text_chunks(ollama_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    chunks = [_make_stream_chunk("Hello"), _make_stream_chunk(" world"), _make_stream_chunk(None)]

    async def _aiter():
        for chunk in chunks:
            yield chunk

    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_aiter())
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(ollama_settings, "ollama")
        messages = [LLMMessage(role=MessageRole.USER, content="Say hi")]
        result = [token async for token in provider.stream(messages)]

    assert result == ["Hello", " world"]


@pytest.mark.asyncio
async def test_complete_passes_tools_in_openai_format(ollama_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    fake_response = _make_chat_completion()

    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(ollama_settings, "ollama")
        messages = [LLMMessage(role=MessageRole.USER, content="Use a tool")]
        tools = [{"name": "search", "description": "Search the web", "input_schema": {"type": "object"}}]
        await provider.complete(messages, tools=tools)

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "tools" in call_kwargs
    assert call_kwargs["tools"][0]["type"] == "function"
    assert call_kwargs["tools"][0]["function"]["name"] == "search"


@pytest.mark.asyncio
async def test_complete_wraps_connection_error(ollama_settings):
    import openai

    from pincer.exceptions import LLMError
    from pincer.llm.openai_common import OpenAICompatibleProvider

    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=openai.APIConnectionError(request=MagicMock()))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(ollama_settings, "ollama")
        messages = [LLMMessage(role=MessageRole.USER, content="hello")]

        with pytest.raises(LLMError, match="connection error"):
            await provider.complete(messages)
