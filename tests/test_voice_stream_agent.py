"""Agent.stream_voice_turn + BaseLLMProvider.stream_turn (Sprint 5, T5.2/T5.4)."""

from __future__ import annotations

from typing import Any

import pytest

from pincer.core.agent import Agent, StreamEventType
from pincer.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    StreamTurnEvent,
    ToolCall,
)


class ScriptedProvider(BaseLLMProvider):
    """stream_turn yields scripted turns; records call kwargs for assertions."""

    def __init__(self, turns: list[list[StreamTurnEvent]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use complete()")

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        raise AssertionError("voice turns must not use stream()")
        yield ""  # pragma: no cover

    async def stream_turn(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls.append(
            {"tools": tools, "model": model, "max_tokens": max_tokens, "system": system, "messages": list(messages)}
        )
        for event in self.turns.pop(0):
            yield event

    async def close(self) -> None:
        pass


class CompleteOnlyProvider(BaseLLMProvider):
    """Exercises the base-class stream_turn fallback (complete()-backed)."""

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        return LLMResponse(content="Full answer at once.", model="m", input_tokens=10, output_tokens=5)

    async def stream(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        yield ""  # pragma: no cover

    async def close(self) -> None:
        pass


def _text_turn(*tokens: str, tool_calls: list[ToolCall] | None = None) -> list[StreamTurnEvent]:
    events = [StreamTurnEvent(text=t) for t in tokens]
    events.append(
        StreamTurnEvent(
            response=LLMResponse(
                content="".join(tokens),
                tool_calls=tool_calls or [],
                model="test-model",
                input_tokens=50,
                output_tokens=20,
            )
        )
    )
    return events


async def _collect(agent: Agent, **kwargs):
    chunks = []
    async for chunk in agent.stream_voice_turn(**kwargs):
        chunks.append(chunk)
    return chunks


class TestBaseStreamTurnFallback:
    async def test_complete_backed_fallback(self):
        provider = CompleteOnlyProvider()
        events = [e async for e in provider.stream_turn(messages=[])]
        assert events[0].text == "Full answer at once."
        assert events[-1].response is not None
        assert events[-1].response.content == "Full answer at once."


class TestStreamVoiceTurn:
    async def test_text_streamed_and_persisted(self, settings, session_manager, cost_tracker, tool_registry):
        settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
        provider = ScriptedProvider([_text_turn("Gerne. ", "Einen Moment bitte.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        chunks = await _collect(agent, user_id="u1", channel="voice", text="Hallo", extra_system="VOICE-MARKER")

        texts = [c.content for c in chunks if c.type == StreamEventType.TEXT]
        assert texts == ["Gerne. ", "Einen Moment bitte."]
        assert chunks[-1].type == StreamEventType.DONE
        assert chunks[-1].content == "Gerne. Einen Moment bitte."

        # extra_system reached the LLM; voice caps applied
        call = provider.calls[0]
        assert "VOICE-MARKER" in call["system"]
        assert call["max_tokens"] == 150
        assert call["model"] is None  # no voice_turn_model configured

        # session persisted: user turn + assistant turn
        session = await session_manager.get_or_create("u1", "voice")
        roles = [m.role.value for m in session.messages]
        assert roles == ["user", "assistant"]
        assert session.messages[-1].content == "Gerne. Einen Moment bitte."

    async def test_voice_turn_model_override(self, settings, session_manager, cost_tracker, tool_registry):
        settings.voice_turn_model = "fast-model-x"
        provider = ScriptedProvider([_text_turn("Ok, mache ich sofort.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        await _collect(agent, user_id="u1", channel="voice", text="Hallo")
        assert provider.calls[0]["model"] == "fast-model-x"

    async def test_tool_iteration_yields_tool_events(self, settings, session_manager, cost_tracker, tool_registry):
        async def fake_tool() -> str:
            return "sunny"

        tool_registry.register("calendar_today", "today's calendar", fake_tool, {"type": "object", "properties": {}})
        tool_call = ToolCall(id="tc1", name="calendar_today", arguments={})
        provider = ScriptedProvider(
            [
                _text_turn(tool_calls=[tool_call]),
                _text_turn("Heute sind zwei Termine im Kalender."),
            ]
        )
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        chunks = await _collect(agent, user_id="u1", channel="voice", text="Was steht heute an?")

        types = [c.type for c in chunks]
        assert types == [
            StreamEventType.TOOL_START,
            StreamEventType.TOOL_DONE,
            StreamEventType.TEXT,
            StreamEventType.DONE,
        ]
        assert chunks[-1].content == "Heute sind zwei Termine im Kalender."

    async def test_tools_filtered_to_voice_set(self, settings, session_manager, cost_tracker, tool_registry):
        async def noop() -> str:
            return ""

        tool_registry.register("calendar_today", "d", noop, {"type": "object", "properties": {}})
        tool_registry.register("shell_exec", "d", noop, {"type": "object", "properties": {}})
        provider = ScriptedProvider([_text_turn("Gerne, kein Problem.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        await _collect(agent, user_id="u1", channel="voice", text="Hallo")

        tool_names = {t["name"] for t in provider.calls[0]["tools"]}
        assert "calendar_today" in tool_names
        assert "shell_exec" not in tool_names  # excluded from live calls

    async def test_cost_recorded(self, settings, session_manager, cost_tracker, tool_registry):
        provider = ScriptedProvider([_text_turn("Gerne, sofort erledigt.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        await _collect(agent, user_id="u1", channel="voice", text="Hallo")
        summary = await cost_tracker.get_summary()
        assert summary.total_calls == 1
        assert summary.total_input_tokens == 50

    async def test_llm_error_propagates_to_channel(self, settings, session_manager, cost_tracker, tool_registry):
        from pincer.exceptions import LLMError

        class BoomProvider(CompleteOnlyProvider):
            async def stream_turn(self, *a, **k):
                raise LLMError("boom")
                yield  # pragma: no cover

        agent = Agent(settings, BoomProvider(), session_manager, cost_tracker, tool_registry)
        with pytest.raises(LLMError):
            await _collect(agent, user_id="u1", channel="voice", text="Hallo")


class TestCrossProviderVoiceModel:
    async def test_provider_prefixed_model_routes_to_that_provider(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        """voice_turn_model="openai:gpt-5-mini" streams the turn on the OpenAI
        provider while the router/default provider is untouched."""
        from pincer.llm.router import LLMRouter

        settings.voice_turn_model = "openai:gpt-5-mini"
        openai_provider = ScriptedProvider([_text_turn("Sure, right away.")])

        router = LLMRouter.__new__(LLMRouter)  # skip __init__ (env-based)
        router._settings = settings
        router._pool = {"anthropic": ScriptedProvider([])}

        def fake_get_provider(name, model_hint=""):
            assert name == "openai"
            assert model_hint == "gpt-5-mini"
            return openai_provider

        router.get_provider = fake_get_provider  # type: ignore[method-assign]

        agent = Agent(settings, router, session_manager, cost_tracker, tool_registry)
        chunks = await _collect(agent, user_id="u1", channel="voice", text="Hi")

        assert chunks[-1].content == "Sure, right away."
        assert openai_provider.calls[0]["model"] == "gpt-5-mini"

    async def test_unavailable_provider_falls_back_to_default(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        from pincer.llm.router import LLMRouter

        settings.voice_turn_model = "openai:gpt-5-mini"
        default_provider = ScriptedProvider([_text_turn("Fallback answer here.")])

        router = LLMRouter.__new__(LLMRouter)
        router._settings = settings
        router._pool = {"anthropic": default_provider}
        router.get_provider = lambda name, model_hint="": None  # type: ignore[method-assign]
        router.stream_turn = default_provider.stream_turn  # type: ignore[method-assign]
        router.is_free = lambda p: False  # type: ignore[method-assign]

        agent = Agent(settings, router, session_manager, cost_tracker, tool_registry)
        chunks = await _collect(agent, user_id="u1", channel="voice", text="Hi")

        assert chunks[-1].content == "Fallback answer here."
        # foreign model id must NOT be sent to the default provider (would 404)
        assert default_provider.calls[0]["model"] is None


class TestVoiceToolCap:
    def test_large_registry_capped_with_priorities(self):
        """OpenAI 400s above 128 tools; the voice filter caps at 100 keeping
        call-relevant tools (regression for the gpt-4o-mini switch failure)."""
        from pincer.voice.voice_tools import MAX_VOICE_TOOLS, filter_voice_tools

        schemas = [{"name": "make_phone_call"}, {"name": "calendar_today"}]
        schemas += [{"name": f"google__manage_slide_deck_{i}"} for i in range(120)]
        schemas += [{"name": "google__check_freebusy"}, {"name": "google__create_event"}]
        schemas += [{"name": f"doctr__ocr_variant_{i}"} for i in range(40)]
        schemas += [{"name": "openweathermap__get_current_weather"}, {"name": "newsapi__top_headlines"}]
        schemas += [{"name": "shell_exec"}]  # excluded outright

        filtered = filter_voice_tools(schemas)
        names = [s["name"] for s in filtered]

        assert len(filtered) == MAX_VOICE_TOOLS <= 128
        assert "shell_exec" not in names
        # curated builtins and call-relevant tools survive the cut
        for keeper in (
            "make_phone_call",
            "calendar_today",
            "google__check_freebusy",
            "google__create_event",
            "openweathermap__get_current_weather",
            "newsapi__top_headlines",
        ):
            assert keeper in names

    def test_small_registry_untouched(self):
        from pincer.voice.voice_tools import filter_voice_tools

        schemas = [{"name": "calendar_today"}, {"name": "google__create_event"}, {"name": "shell_exec"}]
        assert [s["name"] for s in filter_voice_tools(schemas)] == ["calendar_today", "google__create_event"]

    def test_openai_converter_enforces_hard_limit(self):
        from pincer.llm.openai_common import OPENAI_MAX_TOOLS, convert_tools_to_openai

        tools = [{"name": f"t{i}", "description": "", "input_schema": {}} for i in range(160)]
        converted = convert_tools_to_openai(tools)
        assert len(converted) == OPENAI_MAX_TOOLS == 128
