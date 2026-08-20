"""Tests for OpenAICompatibleProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import openai as _openai
import pytest

from pincer.llm.base import LLMMessage, MessageRole

MODULE = "pincer.llm.openai_common"


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
        default_provider="openai",
        default_model="my-model",
    )


@pytest.mark.asyncio
async def test_complete_returns_content(openai_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    fake_response = _make_chat_completion("Hi!")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.content == "Hi!"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.provider == "openai"  # stamps the serving provider for cost attribution


def test_missing_openai_key_raises():
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant",  # type: ignore[arg-type]  (some provider must be set)
        openai_api_key="",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="gpt-4o",
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncOpenAI"), pytest.raises(ValueError, match="PINCER_OPENAI_API_KEY"):
        OpenAICompatibleProvider(settings, "openai")


def test_claude_model_on_openai_wire_raises():
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        openai_api_key="sk-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="openai",
        default_model="claude-sonnet-4-5-20250929",  # claude default with no openai_model
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncOpenAI"), pytest.raises(ValueError, match="PINCER_OPENAI_MODEL"):
        OpenAICompatibleProvider(settings, "openai")


@pytest.mark.asyncio
async def test_well_known_openai_uses_key_and_base_url(openai_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        OpenAICompatibleProvider(openai_settings, "openai")

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["base_url"] == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_compatible_uses_compatible_base_url_and_passthrough_model():
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="grok",
        default_model="grok-3",
        openai_compatible_provider="grok",
        openai_compatible_base_url="https://api.x.ai/v1",
        openai_compatible_api_key="xai-key",  # type: ignore[arg-type]
    )
    fake_response = _make_chat_completion(model="grok-3")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(settings, "grok")
        await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    init_kwargs = mock_cls.call_args.kwargs
    assert init_kwargs["base_url"] == "https://api.x.ai/v1"
    assert init_kwargs["api_key"] == "xai-key"
    # model passed straight through — no remapping
    assert mock_client.chat.completions.create.call_args.kwargs["model"] == "grok-3"


@pytest.mark.asyncio
async def test_per_provider_model_overrides_default():
    """A compatible endpoint uses its own model, not the primary's default_model."""
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        anthropic_api_key="sk-ant",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="claude-sonnet-4-5-20250929",  # primary's model
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        openai_compatible_model="llama3.2",  # failover's own model
        _env_file=None,  # type: ignore[call-arg]
    )
    fake_response = _make_chat_completion(model="llama3.2")

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(settings, "ollama")
        await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    # uses the compatible endpoint's model, NOT default_model (claude-…)
    assert mock_client.chat.completions.create.call_args.kwargs["model"] == "llama3.2"


@pytest.mark.asyncio
async def test_well_known_openai_model_override():
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        openai_api_key="sk-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="anthropic",
        anthropic_api_key="sk-ant",  # type: ignore[arg-type]
        default_model="claude-sonnet-4-5-20250929",
        openai_model="gpt-4o",
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = OpenAICompatibleProvider(settings, "openai")

    assert provider._default_model == "gpt-4o"


@pytest.mark.asyncio
async def test_compatible_empty_key_uses_placeholder():
    from pincer.config import Settings
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = Settings(
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        default_provider="ollama",
        default_model="llama3.2",
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
    )

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        OpenAICompatibleProvider(settings, "ollama")

    assert mock_cls.call_args.kwargs["api_key"] == "none"


@pytest.mark.asyncio
async def test_complete_converts_complex_messages(openai_settings):
    from pincer.llm.base import ImageContent, ToolCall
    from pincer.llm.openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_chat_completion())
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        messages = [
            LLMMessage(role=MessageRole.SYSTEM, content="sys"),
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
            LLMMessage(role=MessageRole.TOOL_RESULT, content="res", tool_call_id="t1"),
        ]
        await provider.complete(messages, system="top-system")

    sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "top-system"}
    roles = [m["role"] for m in sent]
    assert "tool" in roles and "assistant" in roles


@pytest.mark.asyncio
async def test_complete_parses_tool_calls(openai_settings):
    import json

    from pincer.llm.openai_common import OpenAICompatibleProvider

    tc = MagicMock()
    tc.id = "call_1"
    tc.type = "function"
    tc.function.name = "search"
    tc.function.arguments = json.dumps({"q": "x"})

    message = MagicMock()
    message.content = None
    message.tool_calls = [tc]
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "tool_calls"
    usage = MagicMock()
    usage.prompt_tokens = 1
    usage.completion_tokens = 1
    response = MagicMock()
    response.choices = [choice]
    response.model = "my-model"
    response.usage = usage

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=response)
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        result = await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert result.has_tool_calls
    assert result.tool_calls[0].name == "search"
    assert result.tool_calls[0].arguments == {"q": "x"}


@pytest.mark.asyncio
async def test_connection_error_includes_provider_name(openai_settings):
    import openai

    from pincer.exceptions import LLMError
    from pincer.llm.openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=openai.APIConnectionError(request=MagicMock()))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        with pytest.raises(LLMError, match="connection error"):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])


@pytest.mark.asyncio
async def test_complete_api_status_error(openai_settings):
    from pincer.exceptions import LLMError
    from pincer.llm.openai_common import OpenAICompatibleProvider

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=_openai.APIStatusError("bad request", response=MagicMock(status_code=400), body={})
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
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
    from pincer.llm.openai_common import OpenAICompatibleProvider

    chunks = [_make_stream_chunk("Hello"), _make_stream_chunk(" world"), _make_stream_chunk(None)]

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _make_async_iterable(chunks)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        result = [t async for t in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")])]

    assert result == ["Hello", " world"]


@pytest.mark.asyncio
async def test_stream_with_tools(openai_settings):
    from pincer.llm.openai_common import OpenAICompatibleProvider

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

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        tools = [{"name": "my_tool", "description": "a tool", "input_schema": {"type": "object", "properties": {}}}]
        result = [t async for t in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")], tools=tools)]

    assert result == ["ok"]


@pytest.mark.asyncio
async def test_stream_rate_limit_error(openai_settings):
    from pincer.exceptions import LLMRateLimitError
    from pincer.llm.openai_common import OpenAICompatibleProvider

    async def _raise(**kwargs):
        raise _openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body={})

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _raise
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        with pytest.raises(LLMRateLimitError):
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_stream_api_status_error(openai_settings):
    from pincer.exceptions import LLMError
    from pincer.llm.openai_common import OpenAICompatibleProvider

    async def _raise(**kwargs):
        raise _openai.APIStatusError("server error", response=MagicMock(status_code=500), body={})

    with patch(f"{MODULE}.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = _raise
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        with pytest.raises(LLMError, match="500"):
            async for _ in provider.stream([LLMMessage(role=MessageRole.USER, content="hi")]):
                pass


@pytest.mark.asyncio
async def test_retry_on_rate_limit_exhausted(openai_settings):
    from pincer.exceptions import LLMRateLimitError
    from pincer.llm.openai_common import OpenAICompatibleProvider

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

        provider = OpenAICompatibleProvider(openai_settings, "openai")
        with pytest.raises(LLMRateLimitError):
            await provider.complete([LLMMessage(role=MessageRole.USER, content="hi")])

    assert call_count == 3  # max_retries=3


# ── stream_turn: tool-delta assembly + reasoning-model kwargs (Sprint 5) ──


class TestStreamTurn:
    def _provider(self):
        from unittest.mock import MagicMock, patch

        from pincer.config import Settings
        from pincer.llm.openai_common import OpenAICompatibleProvider

        settings = Settings(
            anthropic_api_key="sk-ant-test",  # satisfies at-least-one-provider
            openai_api_key="sk-oai-test",
            telegram_bot_token="123456:TEST",
            openai_model="gpt-4o-mini",
        )
        with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            return OpenAICompatibleProvider(settings, "openai")

    def _chunk(self, content=None, tool_delta=None, finish=None, usage=None):
        from unittest.mock import MagicMock

        chunk = MagicMock()
        chunk.model = "gpt-5-mini"
        chunk.usage = usage
        if content is None and tool_delta is None and finish is None:
            chunk.choices = []
            return chunk
        choice = MagicMock()
        choice.finish_reason = finish
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = tool_delta
        choice.delta = delta
        chunk.choices = [choice]
        return chunk

    def _tool_delta(self, index, id=None, name=None, arguments=None):
        from unittest.mock import MagicMock

        tc = MagicMock()
        tc.index = index
        tc.id = id
        tc.function = MagicMock()
        tc.function.name = name
        tc.function.arguments = arguments
        return tc

    async def test_text_then_final_response(self):
        from unittest.mock import AsyncMock, MagicMock

        provider = self._provider()
        usage = MagicMock(prompt_tokens=100, completion_tokens=12)
        chunks = [self._chunk(content="Hello "), self._chunk(content="there."), self._chunk(finish="stop", usage=usage)]

        async def fake_stream(**kwargs):
            for c in chunks:
                yield c

        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(side_effect=lambda **kw: fake_stream(**kw))

        events = [e async for e in provider.stream_turn(messages=[], max_tokens=150)]
        texts = [e.text for e in events if e.response is None]
        assert texts == ["Hello ", "there."]
        final = events[-1].response
        assert final.content == "Hello there."
        assert final.input_tokens == 100
        assert final.provider == "openai"

    async def test_tool_call_deltas_assembled(self):
        from unittest.mock import AsyncMock, MagicMock

        provider = self._provider()
        chunks = [
            self._chunk(tool_delta=[self._tool_delta(0, id="call_1", name="calendar_today", arguments='{"da')]),
            self._chunk(tool_delta=[self._tool_delta(0, arguments='y": "mon"}')]),
            self._chunk(finish="tool_calls"),
        ]

        async def fake_stream(**kwargs):
            for c in chunks:
                yield c

        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(side_effect=lambda **kw: fake_stream(**kw))

        events = [e async for e in provider.stream_turn(messages=[])]
        final = events[-1].response
        assert len(final.tool_calls) == 1
        assert final.tool_calls[0].id == "call_1"
        assert final.tool_calls[0].name == "calendar_today"
        assert final.tool_calls[0].arguments == {"day": "mon"}
        assert final.stop_reason == "tool_calls"

    async def test_reasoning_model_kwargs_normalized(self):
        from unittest.mock import AsyncMock, MagicMock

        provider = self._provider()

        async def fake_stream(**kwargs):
            yield self._chunk(content="ok, done here now", finish="stop")

        provider._client = MagicMock()
        create = AsyncMock(side_effect=lambda **kw: fake_stream(**kw))
        provider._client.chat.completions.create = create

        _ = [e async for e in provider.stream_turn(messages=[], model="gpt-5-mini", max_tokens=150, temperature=0.5)]
        kwargs = create.call_args.kwargs
        assert kwargs["max_completion_tokens"] == 150
        assert "max_tokens" not in kwargs
        assert "temperature" not in kwargs  # reasoning models accept only default

        _ = [e async for e in provider.stream_turn(messages=[], model="gpt-4o-mini", max_tokens=150)]
        kwargs = create.call_args.kwargs
        assert kwargs["max_tokens"] == 150
