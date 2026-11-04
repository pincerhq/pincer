"""A response cut off by max_tokens never runs its tool calls, on any agent loop (issue #345)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.config import Settings
from pincer.core.agent import Agent, StreamChunk, StreamEventType
from pincer.exceptions import LLMError, LLMOutputTruncatedError
from pincer.llm.base import BaseLLMProvider, LLMResponse, MessageRole, StreamTurnEvent, ToolCall
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.tools.registry import ToolRegistry

_DONE = LLMResponse(content="done", model="test-model", input_tokens=10, output_tokens=5, stop_reason="end_turn")
_CUT_OFF_CALL = ToolCall(id="tc1", name="create_event", arguments={"title": "Dentist"})


def _cut_off_tool_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[_CUT_OFF_CALL],
        model="test-model",
        input_tokens=50,
        output_tokens=4096,
        stop_reason="max_tokens",
    )


class _ScriptedVoiceProvider(BaseLLMProvider):
    """stream_turn replays scripted turns, one per request."""

    def __init__(self, turns: list[list[StreamTurnEvent]]) -> None:
        self.turns = list(turns)

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use stream()")
        yield ""  # pragma: no cover

    async def stream_turn(
        self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None, reasoning_effort=None
    ):
        for event in self.turns.pop(0):
            yield event

    async def close(self) -> None:
        pass


class _RecordingTool:
    """A registry with one tool that records every run; no argument is required,
    so even an empty-argument call would really execute."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.registry = ToolRegistry()

        async def create_event(title: str = "") -> str:
            self.runs.append({"title": title})
            return "created"

        self.registry.register(name="create_event", description="Create a calendar event", handler=create_event)


@pytest.fixture
def recording() -> _RecordingTool:
    return _RecordingTool()


async def test_chat_turn_never_runs_a_cut_off_tool_call(settings, mock_llm, session_manager, cost_tracker, recording):
    mock_llm.complete.side_effect = [_cut_off_tool_response(), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, recording.registry)

    with (
        patch.object(recording.registry, "execute", wraps=recording.registry.execute) as execute,
        pytest.raises(LLMOutputTruncatedError) as excinfo,
    ):
        await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    assert isinstance(excinfo.value, LLMError)
    assert recording.runs == []
    execute.assert_not_awaited()
    session = await session_manager.get_or_create("user1", "test")
    assert [m.role for m in session.messages] == [MessageRole.USER]


async def test_chat_turn_delivers_cut_off_text_unchanged(settings, mock_llm, session_manager, cost_tracker, recording):
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="Here is the first half of a long ans",
            model="test-model",
            input_tokens=50,
            output_tokens=4096,
            stop_reason="max_tokens",
        )
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, recording.registry)

    result = await agent.handle_message("user1", "test", "Tell me everything", memory=False)

    assert result.text == "Here is the first half of a long ans"


async def test_headless_run_never_runs_a_cut_off_tool_call(
    settings, mock_llm, session_manager, cost_tracker, recording, caplog
):
    mock_llm.complete.side_effect = [_cut_off_tool_response(), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, recording.registry)

    with (
        patch.object(recording.registry, "execute", wraps=recording.registry.execute) as execute,
        caplog.at_level(logging.WARNING, logger="pincer.core.agent"),
    ):
        result = await agent.run_headless("Book the dentist", "user1", "telegram")

    assert result == "Sorry, I couldn't complete this scheduled action due to an LLM error."
    assert recording.runs == []
    execute.assert_not_awaited()
    records = [r for r in caplog.records if r.name == "pincer.core.agent" and r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "create_event" in records[0].getMessage()
    assert "Dentist" not in records[0].getMessage()  # tool arguments are never logged


async def test_streaming_chat_turn_never_runs_a_cut_off_tool_call(
    settings, mock_llm, session_manager, cost_tracker, recording
):
    mock_llm.complete.side_effect = [_cut_off_tool_response(), _DONE]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, recording.registry)
    chunks: list[StreamChunk] = []

    with (
        patch.object(recording.registry, "execute", wraps=recording.registry.execute) as execute,
        pytest.raises(LLMOutputTruncatedError),
    ):
        async for chunk in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False):
            chunks.append(chunk)

    assert recording.runs == []
    execute.assert_not_awaited()
    assert StreamEventType.TOOL_START not in [c.type for c in chunks]
    session = await session_manager.get_or_create("user1", "web")
    assert [m.role for m in session.messages] == [MessageRole.USER]


async def test_voice_turn_never_runs_a_cut_off_tool_call(settings, session_manager, cost_tracker, recording):
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    provider = _ScriptedVoiceProvider(
        [
            [StreamTurnEvent(text="Einen Moment. "), StreamTurnEvent(response=_cut_off_tool_response())],
            [StreamTurnEvent(text="Erledigt."), StreamTurnEvent(response=_DONE)],
        ]
    )
    agent = Agent(settings, provider, session_manager, cost_tracker, recording.registry)

    with (
        patch.object(recording.registry, "execute", wraps=recording.registry.execute) as execute,
        pytest.raises(LLMOutputTruncatedError),
    ):
        async for _chunk in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt"):
            pass

    assert recording.runs == []
    execute.assert_not_awaited()
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert [m.role for m in session.messages] == [MessageRole.USER]


def _openai_chunk(*, tool_args: str | None = None, finish: str | None = None) -> SimpleNamespace:
    tool_delta = None
    if tool_args is not None:
        tool_delta = [
            SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="create_event", arguments=tool_args))
        ]
    delta = SimpleNamespace(content=None, tool_calls=tool_delta)
    usage = SimpleNamespace(prompt_tokens=50, completion_tokens=220) if finish else None
    return SimpleNamespace(
        model="gpt-4o-mini", usage=usage, choices=[SimpleNamespace(delta=delta, finish_reason=finish)]
    )


async def test_openai_voice_turn_never_runs_a_tool_call_with_cut_off_arguments(
    settings, session_manager, cost_tracker, recording
):
    """The arguments stop mid-JSON, so they don't parse and fall back to {}:
    without the length → max_tokens mapping that {} call would run."""
    settings.voice_turn_model = ""
    provider_settings = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        openai_api_key="sk-oai-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        openai_model="gpt-4o-mini",
    )
    with patch("pincer.llm.openai_common.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = OpenAICompatibleProvider(provider_settings, "openai")
    streams = [
        [_openai_chunk(tool_args='{"title": "Dent'), _openai_chunk(finish="length")],
        [_openai_chunk(finish="stop")],
    ]

    async def fake_stream(**kwargs):
        for chunk in streams.pop(0):
            yield chunk

    provider._client.chat.completions.create = AsyncMock(side_effect=lambda **kw: fake_stream(**kw))
    agent = Agent(settings, provider, session_manager, cost_tracker, recording.registry)

    with (
        patch.object(recording.registry, "execute", wraps=recording.registry.execute) as execute,
        pytest.raises(LLMOutputTruncatedError),
    ):
        async for _chunk in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt"):
            pass

    assert recording.runs == []
    execute.assert_not_awaited()
