"""Tests for OpenAICompatibleProvider base class."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
def base_settings():
    from pincer.config import Settings

    return Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_model="my-model",
        ollama_base_url="http://localhost:11434/v1",
    )


@pytest.mark.asyncio
async def test_complete_returns_content(base_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    class StubProvider(OpenAICompatibleProvider):
        def __init__(self, settings):
            super().__init__("key", None, settings)

    fake_response = _make_chat_completion("Hi!")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = StubProvider(base_settings)
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "Hi!"
    assert result.input_tokens == 10
    assert result.output_tokens == 5


@pytest.mark.asyncio
async def test_model_map_remaps_model_name(base_settings):
    from pincer.llm._openai_common import OpenAICompatibleProvider

    class MappedProvider(OpenAICompatibleProvider):
        MODEL_MAP = {"claude-sonnet-4-5-20250929": "gpt-4o"}

        def __init__(self, settings):
            super().__init__("key", None, settings)

    fake_response = _make_chat_completion(model="gpt-4o")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        settings = base_settings
        settings.__dict__["default_model"] = "claude-sonnet-4-5-20250929"
        provider = MappedProvider(settings)
        await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_connection_error_includes_provider_name(base_settings):
    import openai

    from pincer.exceptions import LLMError
    from pincer.llm._openai_common import OpenAICompatibleProvider

    class AcmeProvider(OpenAICompatibleProvider):
        def __init__(self, settings):
            super().__init__("key", None, settings)

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=openai.APIConnectionError(request=MagicMock())
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = AcmeProvider(base_settings)
        with pytest.raises(LLMError, match="Acme connection error"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])
