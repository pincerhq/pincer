"""Calls are priced by the model name Pincer sends, not the one the API reports back (issue #357).

OpenAI answers `gpt-4o` as `gpt-4o-2024-08-06`, Azure answers a deployment name with the
underlying model, so every leaf stamps the name it sent on the response: a real leaf never
leaves `request_model` empty.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import Message, TextBlock, Usage

from pincer.config import Settings
from pincer.llm.base import LLMMessage, MessageRole

_HI = [LLMMessage(role=MessageRole.USER, content="hi")]


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def _anthropic_message(model: str) -> Message:
    return Message.model_construct(
        content=[TextBlock.model_construct(type="text", text="Hi!")],
        model=model,
        usage=Usage.model_construct(input_tokens=10, output_tokens=5),
        stop_reason="end_turn",
    )


def _anthropic_stream(message: Message) -> MagicMock:
    stream = MagicMock()
    stream.get_final_message = AsyncMock(return_value=message)

    async def _no_text():  # type: ignore[no-untyped-def]
        return
        yield

    stream.text_stream = _no_text()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture
def anthropic_leaf():  # type: ignore[no-untyped-def]
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    with patch("pincer.llm.anthropic_common.AsyncAnthropic") as mock_cls:
        client = MagicMock()
        client.messages.stream = MagicMock(side_effect=lambda **kw: _anthropic_stream(_anthropic_message("api-name")))
        mock_cls.return_value = client
        yield AnthropicCompatibleProvider(
            _settings(anthropic_api_key="sk-ant-test", default_provider="anthropic", default_model="claude-sent"),
            "anthropic",
        )


async def test_anthropic_complete_stamps_the_model_it_sent(anthropic_leaf) -> None:  # type: ignore[no-untyped-def]
    default = await anthropic_leaf.complete(_HI)
    explicit = await anthropic_leaf.complete(_HI, model="claude-other")
    assert (default.model, default.request_model) == ("api-name", "claude-sent")
    assert explicit.request_model == "claude-other"


async def test_anthropic_stream_stamps_the_model_it_sent(anthropic_leaf) -> None:  # type: ignore[no-untyped-def]
    events = [e async for e in anthropic_leaf.stream_turn(_HI, model="claude-other")]
    final = events[-1].response
    assert final is not None
    assert (final.model, final.request_model) == ("api-name", "claude-other")


def _chat_completion(model: str) -> MagicMock:
    message = MagicMock(content="Hi!", tool_calls=None)
    usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], model=model, usage=usage)


def _chunk(model: str, content: str | None) -> MagicMock:
    delta = MagicMock(content=content, tool_calls=None)
    choice = MagicMock(delta=delta, finish_reason="stop")
    return MagicMock(choices=[choice], model=model, usage=MagicMock(prompt_tokens=10, completion_tokens=5))


@pytest.fixture
def openai_leaf():  # type: ignore[no-untyped-def]
    from pincer.llm.openai_common import OpenAICompatibleProvider

    async def create(**kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("stream"):

            async def gen():  # type: ignore[no-untyped-def]
                yield _chunk("gpt-dated-2026-01-01", "Hi!")

            return gen()
        return _chat_completion("gpt-dated-2026-01-01")

    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        client = MagicMock()
        client.chat.completions.create = create
        client.close = AsyncMock()
        mock_cls.return_value = client
        yield OpenAICompatibleProvider(
            _settings(
                openai_api_key="sk-test",
                default_provider="openai",
                default_model="gpt-sent",
                responses_api_providers=[],  # this test is about Chat Completions
            ),
            "openai",
        )


async def test_openai_complete_stamps_the_model_it_sent(openai_leaf) -> None:  # type: ignore[no-untyped-def]
    default = await openai_leaf.complete(_HI)
    explicit = await openai_leaf.complete(_HI, model="my-deployment")
    assert (default.model, default.request_model) == ("gpt-dated-2026-01-01", "gpt-sent")
    assert explicit.request_model == "my-deployment"


async def test_openai_stream_stamps_the_model_it_sent(openai_leaf) -> None:  # type: ignore[no-untyped-def]
    events = [e async for e in openai_leaf.stream_turn(_HI, model="my-deployment")]
    final = events[-1].response
    assert final is not None
    assert (final.model, final.request_model) == ("gpt-dated-2026-01-01", "my-deployment")


_SENT_AND_REPORTED = {"model": "api-dated-name", "request_model": "test-fixture-model"}


def _llm_answering(**names: str):  # type: ignore[no-untyped-def]
    from collections.abc import AsyncIterator
    from typing import Any

    from pincer.llm.base import BaseLLMProvider, LLMResponse, StreamTurnEvent

    def response() -> LLMResponse:
        return LLMResponse(content="Hi.", input_tokens=1_000_000, output_tokens=0, stop_reason="end_turn", **names)

    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.side_effect = lambda *a, **k: response()

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(text="Hi.")
        yield StreamTurnEvent(response=response())

    llm.stream_turn = stream_turn
    return llm


@pytest.mark.parametrize("path", ["chat", "headless", "voice"])
async def test_the_agent_records_a_call_under_the_model_it_sent(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry, path: str
) -> None:
    from datetime import UTC, datetime

    from pincer.core.agent import Agent

    agent = Agent(settings, _llm_answering(**_SENT_AND_REPORTED), session_manager, cost_tracker, tool_registry)
    if path == "chat":
        await agent.handle_message("user", "chat", "hi", memory=False)
    elif path == "headless":
        await agent.run_headless("hi", "user", "chat")
    else:
        [chunk async for chunk in agent.stream_voice_turn("user", "voice", "hi")]

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    rows = await cost_tracker.get_costs_by_model(today, today)
    assert [(r["model"], r["total"]) for r in rows] == [("test-fixture-model", 7.0)]  # $7 per MTok input


async def test_the_agent_falls_back_to_the_reported_model_only_without_a_sent_one(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry
) -> None:
    """Test doubles that build an LLMResponse by hand carry no request_model; a real leaf always does."""
    from datetime import UTC, datetime

    from pincer.core.agent import Agent

    agent = Agent(settings, _llm_answering(model="test-fixture-model"), session_manager, cost_tracker, tool_registry)
    await agent.handle_message("user", "chat", "hi", memory=False)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert [r["model"] for r in await cost_tracker.get_costs_by_model(today, today)] == ["test-fixture-model"]
