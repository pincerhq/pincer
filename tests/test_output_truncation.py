"""A response cut off by max_tokens never runs its tool calls, on any agent loop (issue #345)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.config import Settings
from pincer.core.agent import Agent, StreamChunk, StreamEventType
from pincer.db import ensure_schema_current
from pincer.exceptions import LLMError, LLMOutputTruncatedError
from pincer.llm.base import BaseLLMProvider, LLMResponse, MessageRole, StreamTurnEvent, ToolCall
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.tools.registry import ToolRegistry
from pincer.voice.telemetry import queries, runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.tracer import bind_turn_tracer, drain_pending

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
        self.requests: list[dict[str, Any]] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use stream()")
        yield ""  # pragma: no cover

    async def stream_turn(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=None,
        temperature=None,
        system=None,
        reasoning_effort=None,
        thinking=None,
    ):
        self.requests.append({"max_tokens": max_tokens, "messages": [m.content for m in messages]})
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
    assert len(provider.requests) == 1  # text was already spoken: no silent retry
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
    # Nothing is spoken, so the voice turn retries once with a bigger limit;
    # the retry is cut off the same way.
    streams = [
        [_openai_chunk(tool_args='{"title": "Dent'), _openai_chunk(finish="length")],
        [_openai_chunk(tool_args='{"title": "Dentist", "whe'), _openai_chunk(finish="length")],
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


# ── Voice: silent retry and the truncation policy (slice 2, Q8d) ──────────────


def _voice_text(*tokens: str, truncated: bool = False) -> list[StreamTurnEvent]:
    events = [StreamTurnEvent(text=t) for t in tokens]
    events.append(
        StreamTurnEvent(
            response=LLMResponse(
                content="".join(tokens),
                model="test-model",
                input_tokens=50,
                output_tokens=220 if truncated else 12,
                stop_reason="max_tokens" if truncated else "end_turn",
            )
        )
    )
    return events


def _voice_cut_off_tool() -> list[StreamTurnEvent]:
    return [StreamTurnEvent(response=_cut_off_tool_response())]


def _voice_tool_call() -> list[StreamTurnEvent]:
    return [
        StreamTurnEvent(
            response=LLMResponse(
                tool_calls=[ToolCall(id="tc2", name="create_event", arguments={"title": "Dentist"})],
                model="test-model",
                input_tokens=50,
                output_tokens=30,
                stop_reason="tool_use",
            )
        )
    ]


def _voice_agent(settings, session_manager, cost_tracker, recording, turns, *, limit: int = 220):
    settings.voice_turn_model = ""
    settings.voice_max_response_tokens = limit
    provider = _ScriptedVoiceProvider(turns)
    return Agent(settings, provider, session_manager, cost_tracker, recording.registry), provider


async def _voice_chunks(agent: Agent, text: str = "Termin beim Zahnarzt") -> list[StreamChunk]:
    return [c async for c in agent.stream_voice_turn("+4930111222", "voice", text)]


async def test_voice_cut_off_with_nothing_spoken_is_retried_silently(
    settings, session_manager, cost_tracker, recording
):
    agent, provider = _voice_agent(
        settings, session_manager, cost_tracker, recording, [_voice_cut_off_tool(), _voice_text("Gerne.")]
    )

    chunks = await _voice_chunks(agent)

    assert [r["max_tokens"] for r in provider.requests] == [220, 880]
    assert provider.requests[1]["messages"] == provider.requests[0]["messages"]
    assert [c.content for c in chunks if c.type == StreamEventType.TEXT] == ["Gerne."]
    assert StreamEventType.TOOL_START not in [c.type for c in chunks]
    assert recording.runs == []
    session = await session_manager.get_or_create("+4930111222", "voice")
    assert [m.role for m in session.messages] == [MessageRole.USER, MessageRole.ASSISTANT]


async def test_voice_retry_that_is_also_cut_off_raises(settings, session_manager, cost_tracker, recording):
    agent, provider = _voice_agent(
        settings, session_manager, cost_tracker, recording, [_voice_cut_off_tool(), _voice_cut_off_tool()]
    )

    with pytest.raises(LLMOutputTruncatedError) as excinfo:
        await _voice_chunks(agent)

    assert [r["max_tokens"] for r in provider.requests] == [220, 880]
    assert excinfo.value.max_tokens == 880
    assert recording.runs == []


async def test_voice_retry_cut_off_with_nothing_said_raises(settings, session_manager, cost_tracker, recording):
    agent, provider = _voice_agent(
        settings,
        session_manager,
        cost_tracker,
        recording,
        [_voice_text(truncated=True), _voice_text(truncated=True)],
    )

    with pytest.raises(LLMOutputTruncatedError):
        await _voice_chunks(agent)

    assert [r["max_tokens"] for r in provider.requests] == [220, 880]


async def test_voice_cut_off_at_the_ceiling_is_not_retried(settings, session_manager, cost_tracker, recording):
    agent, provider = _voice_agent(
        settings, session_manager, cost_tracker, recording, [_voice_cut_off_tool()], limit=128_000
    )

    with pytest.raises(LLMOutputTruncatedError):
        await _voice_chunks(agent)

    assert [r["max_tokens"] for r in provider.requests] == [128_000]
    assert recording.runs == []


async def test_voice_only_the_retried_request_gets_the_bigger_limit(settings, session_manager, cost_tracker, recording):
    agent, provider = _voice_agent(
        settings,
        session_manager,
        cost_tracker,
        recording,
        [_voice_cut_off_tool(), _voice_tool_call(), _voice_text("Eingetragen."), _voice_text("Bitte.")],
    )

    await _voice_chunks(agent)
    await _voice_chunks(agent, "Danke")

    assert [r["max_tokens"] for r in provider.requests] == [220, 880, 220, 220]
    assert recording.runs == [{"title": "Dentist"}]  # the retry's complete tool call ran


async def test_voice_cut_off_after_earlier_speech_still_raises(settings, session_manager, cost_tracker, recording):
    """ "Let me check" was spoken and a tool ran; the next request and its retry
    are both cut off before saying anything. The caller must not be left with a
    promise and silence: the turn raises (lead ruling on Q8d)."""
    agent, provider = _voice_agent(
        settings,
        session_manager,
        cost_tracker,
        recording,
        [
            [StreamTurnEvent(text="Ich schaue nach. "), *_voice_tool_call()],
            _voice_text(truncated=True),
            _voice_text(truncated=True),
        ],
    )

    with pytest.raises(LLMOutputTruncatedError):
        await _voice_chunks(agent)

    assert [r["max_tokens"] for r in provider.requests] == [220, 220, 880]


@pytest.fixture
async def telemetry(tmp_path):
    db_path = tmp_path / "telephony.db"
    ensure_schema_current(db_path)
    runtime.reset_for_tests(db_path=str(db_path))
    await get_recorder().start()
    yield db_path
    await runtime.shutdown()
    runtime.reset_for_tests()


async def _traced_voice_turn(agent: Agent, call_sid: str) -> list[StreamChunk]:
    tracer = runtime.start_call(provider_call_id=call_sid, direction="inbound", engine="media_streams")
    assert tracer is not None
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    with bind_turn_tracer(turn):
        chunks = await _voice_chunks(agent)
    turn.finish()
    await tracer.finish(status="completed", failure_code="none")
    await drain_pending()
    await get_recorder().flush()
    return chunks


async def test_voice_cut_off_mid_sentence_ends_the_turn_with_what_was_said(
    telemetry, settings, session_manager, cost_tracker, recording, caplog
):
    agent, provider = _voice_agent(
        settings, session_manager, cost_tracker, recording, [_voice_text("Ihr Termin ist am ", truncated=True)]
    )

    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"):
        chunks = await _traced_voice_turn(agent, "CA_midsentence")

    assert chunks[-1].type == StreamEventType.DONE
    assert chunks[-1].content == "Ihr Termin ist am "
    assert len(provider.requests) == 1
    warnings = [r for r in caplog.records if r.name == "pincer.core.agent" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "220 of 220" in warnings[0].getMessage()
    call = await queries.get_call(telemetry, "CA_midsentence")
    events = await queries.get_events(telemetry, call["call_id"])
    cut = [e for e in events if e["name"] == "llm.output_truncated"]
    assert len(cut) == 1
    assert cut[0]["attributes"]["max_tokens"] == 220


async def test_voice_silent_retry_is_recorded_in_telemetry(
    telemetry, settings, session_manager, cost_tracker, recording
):
    agent, _provider = _voice_agent(
        settings, session_manager, cost_tracker, recording, [_voice_cut_off_tool(), _voice_text("Gerne.")]
    )

    await _traced_voice_turn(agent, "CA_retry")

    call = await queries.get_call(telemetry, "CA_retry")
    assert call["retry_count"] == 1
    events = await queries.get_events(telemetry, call["call_id"])
    retries = [e for e in events if e["attributes"].get("code") == "llm_retry"]
    assert len(retries) == 1
    assert retries[0]["attributes"]["attempt"] == 2
    assert retries[0]["attributes"]["max_tokens"] == 880
