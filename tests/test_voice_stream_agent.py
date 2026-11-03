"""Agent.stream_voice_turn + BaseLLMProvider.stream_turn (Sprint 5, T5.2/T5.4)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from pincer.core.agent import Agent, StreamEventType
from pincer.exceptions import LLMError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
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
        assert call["max_tokens"] == 220
        assert call["model"] is None  # no voice_turn_model configured

        # session persisted: user turn + assistant turn
        session = await session_manager.get_or_create("u1", "voice")
        roles = [m.role.value for m in session.messages]
        assert roles == ["user", "assistant"]
        assert session.messages[-1].content == "Gerne. Einen Moment bitte."

    async def test_concurrent_calls_to_one_number_keep_separate_histories(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        """An inbound call while an outbound one to the same number is live:
        neither call sees, nor clears, the other's turns."""
        settings.voice_turn_model = ""
        provider = ScriptedProvider(
            [_text_turn("Es geht um die Lieferung."), _text_turn("Praxis Pincer."), _text_turn("Und Dienstag?")]
        )
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        number = "+4930111222"

        await _collect(agent, user_id=number, channel="voice", text="Hallo, Anruf A", session_key="voice:CA_a")
        await _collect(agent, user_id=number, channel="voice", text="Hallo, Anruf B", session_key="voice:CA_b")
        await _collect(agent, user_id=number, channel="voice", text="Weiter in A", session_key="voice:CA_a")

        assert [m.content for m in provider.calls[1]["messages"]] == ["Hallo, Anruf B"]
        assert [m.content for m in provider.calls[2]["messages"]] == [
            "Hallo, Anruf A",
            "Es geht um die Lieferung.",
            "Weiter in A",
        ]

    async def test_blocking_turn_shares_the_call_session_and_never_onboards(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        """Guard regeneration goes through handle_message under the resolved
        identity; the call's session_key keeps it in the call's own history,
        and the fresh per-call session must not trigger the onboarding question."""

        class BlockingProvider(ScriptedProvider):
            async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
                self.calls.append({"messages": list(messages)})
                return LLMResponse(content="Gern auf Deutsch.", model="m", input_tokens=1, output_tokens=1)

        settings.voice_turn_model = ""
        provider = BlockingProvider([_text_turn("Sure, in English.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        await _collect(agent, user_id="+4930111222", channel="voice", text="Hallo", session_key="voice:CA_a")
        result = await agent.handle_message(
            user_id="voice:+4930111222",  # what IdentityMiddleware resolves an unmapped number to
            channel="voice",
            channel_name="voice",
            text="[Reply in German]",
            session_key="voice:CA_a",
        )

        assert [m.content for m in provider.calls[1]["messages"]] == ["Hallo", "Sure, in English.", "[Reply in German]"]
        assert result.text == "Gern auf Deutsch."  # no onboarding question appended

    async def test_end_voice_session_persists_and_evicts(self, settings, session_manager, cost_tracker, tool_registry):
        """A key per call must not grow the session cache forever: call end
        drops the session, with its history kept in the store."""
        settings.voice_turn_model = ""
        provider = ScriptedProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        await _collect(agent, user_id="+4930111222", channel="voice", text="Hallo", session_key="voice:CA_a")
        await agent.end_voice_session("voice:CA_a")

        assert "voice:CA_a" not in session_manager._cache
        reloaded = await session_manager.get_or_create("+4930111222", "voice", session_key="voice:CA_a")
        assert [m.content for m in reloaded.messages] == ["Hallo", "Gerne."]

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

        tool_registry.register("google__list_events", "d", noop, {"type": "object", "properties": {}})
        tool_registry.register("shell_exec", "d", noop, {"type": "object", "properties": {}})
        provider = ScriptedProvider([_text_turn("Gerne, kein Problem.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        await _collect(agent, user_id="u1", channel="voice", text="Hallo")

        tool_names = {t["name"] for t in provider.calls[0]["tools"]}
        assert "google__list_events" in tool_names
        assert "shell_exec" not in tool_names  # excluded from live calls

    async def test_cost_recorded(self, settings, session_manager, cost_tracker, tool_registry):
        provider = ScriptedProvider([_text_turn("Gerne, sofort erledigt.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        await _collect(agent, user_id="u1", channel="voice", text="Hallo")
        summary = await cost_tracker.get_summary()
        assert summary.total_calls == 1
        assert summary.total_input_tokens == 50

    async def test_llm_error_propagates_to_channel(self, settings, session_manager, cost_tracker, tool_registry):
        class BoomProvider(CompleteOnlyProvider):
            async def stream_turn(self, *a, **k):
                raise LLMError("boom")
                yield  # pragma: no cover

        agent = Agent(settings, BoomProvider(), session_manager, cost_tracker, tool_registry)
        with pytest.raises(LLMError):
            await _collect(agent, user_id="u1", channel="voice", text="Hallo")

    async def test_tool_pair_error_sanitizes_and_retries(self, settings, session_manager, cost_tracker, tool_registry):
        """A history that turns bad between the proactive pass and the call is
        repaired once the provider rejects it; the retry no longer carries the
        orphan. Stream errors now carry the provider's message, so the voice
        path can tell a tool-pairing 400 apart."""
        settings.voice_turn_model = ""

        class RejectsOnceProvider(ScriptedProvider):
            async def stream_turn(
                self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None
            ):
                if not self.calls:
                    self.calls.append({"messages": list(messages)})
                    late = ToolCall(id="late", name="greet", arguments={})
                    messages.append(LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[late]))
                    raise LLMError(
                        "openai stream error: 400 An assistant message with 'tool_calls' must be followed by "
                        "tool messages responding to each 'tool_call_id'. The following tool_call_ids did not "
                        "have response messages: late"
                    )
                async for event in super().stream_turn(messages, tools, model, max_tokens, temperature, system):
                    yield event

        provider = RejectsOnceProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        chunks = await _collect(agent, user_id="u1", channel="voice", text="Hallo")

        assert chunks[-1].type == StreamEventType.DONE
        assert chunks[-1].content == "Gerne."
        assert len(provider.calls) == 2
        retried = provider.calls[1]["messages"]
        assert not any(m.tool_calls for m in retried)


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
        router._unavailable, router._voice_warned = set(), set()

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
        router._unavailable, router._voice_warned = set(), set()
        router.get_provider = lambda name, model_hint="": None  # type: ignore[method-assign]
        router.stream_turn = default_provider.stream_turn  # type: ignore[method-assign]
        router.is_free = lambda p: False  # type: ignore[method-assign]

        agent = Agent(settings, router, session_manager, cost_tracker, tool_registry)
        chunks = await _collect(agent, user_id="u1", channel="voice", text="Hi")

        assert chunks[-1].content == "Fallback answer here."
        # foreign model id must NOT be sent to the default provider (would 404)
        assert default_provider.calls[0]["model"] is None

    async def test_stale_override_without_key_runs_on_default_every_turn(
        self, settings, session_manager, cost_tracker, tool_registry, caplog
    ):
        """#305: a stored openai:… override with no OpenAI key — the real router
        resolution falls back to the default provider and model, and says so once."""
        import logging

        from pincer.llm.router import LLMRouter

        settings.voice_turn_model = "openai:gpt-5-mini"
        settings.openai_api_key = SecretStr("")
        default_provider = ScriptedProvider([_text_turn("First answer here."), _text_turn("Second answer here.")])

        router = LLMRouter.__new__(LLMRouter)
        router._settings = settings
        router._pool = {"anthropic": default_provider}
        router._unavailable, router._voice_warned = set(), set()
        router.stream_turn = default_provider.stream_turn  # type: ignore[method-assign]
        router.is_free = lambda p: False  # type: ignore[method-assign]

        caplog.set_level(logging.WARNING, logger="pincer.llm.router")
        agent = Agent(settings, router, session_manager, cost_tracker, tool_registry)
        await _collect(agent, user_id="u1", channel="voice", text="Hi")
        await _collect(agent, user_id="u1", channel="voice", text="Again")

        assert [c["model"] for c in default_provider.calls] == [None, None]
        assert router._unavailable == {"openai"}
        warnings = [r.getMessage() for r in caplog.records if r.name == "pincer.llm.router"]
        assert len([w for w in warnings if "per-call override" in w]) == 1
        assert len([w for w in warnings if "voice_turn_model" in w]) == 1


class TestVoiceToolCap:
    def test_large_registry_capped_with_priorities(self):
        """OpenAI 400s above 128 tools; the voice filter caps at 100 keeping
        call-relevant tools (regression for the gpt-4o-mini switch failure)."""
        from pincer.voice.voice_tools import MAX_VOICE_TOOLS, filter_voice_tools

        schemas = [{"name": "make_phone_call"}, {"name": "google__list_events"}]
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
            "google__list_events",
            "google__check_freebusy",
            "google__create_event",
            "openweathermap__get_current_weather",
            "newsapi__top_headlines",
        ):
            assert keeper in names

    def test_small_registry_untouched(self):
        from pincer.voice.voice_tools import filter_voice_tools

        schemas = [{"name": "google__list_events"}, {"name": "google__create_event"}, {"name": "shell_exec"}]
        assert [s["name"] for s in filter_voice_tools(schemas)] == ["google__list_events", "google__create_event"]

    def test_openai_converter_enforces_hard_limit(self):
        from pincer.llm.openai_common import OPENAI_MAX_TOOLS, convert_tools_to_openai

        tools = [{"name": f"t{i}", "description": "", "input_schema": {}} for i in range(160)]
        converted = convert_tools_to_openai(tools)
        assert len(converted) == OPENAI_MAX_TOOLS == 128


# ── Issue #300: the prompt follows the offered tools; no repeated calls ──


class _MCPStub:
    """list_servers / tool_names as MCPClientManager reports them."""

    def __init__(self, servers: dict[str, list[str]]) -> None:
        self.servers = servers

    async def list_servers(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "connected": True, "tool_count": len(tools), "instructions": f"{name} server notes"}
            for name, tools in self.servers.items()
        ]

    def tool_names(self, server_name: str) -> list[str]:
        return list(self.servers.get(server_name, []))


def _call_gate(settings, allowed: set[str], language: str = "de"):
    from pincer.voice.engine import CallDirection, CallState
    from pincer.voice.in_call_tools import InCallToolGate
    from pincer.voice.state_machine import CallStateMachine

    state = CallState(call_sid="CA300", direction=CallDirection.OUTBOUND, caller_number="+4952230000")
    state.language = language
    return InCallToolGate(
        call_sid="CA300",
        state=state,
        sm=CallStateMachine("CA300", is_outbound=True),
        settings=settings,
        allowed_tools=frozenset(allowed),
    )


def _register(tool_registry, *names: str, calls: list[str] | None = None) -> None:
    for name in names:

        async def handler(_name: str = name, **kwargs: Any) -> str:
            if calls is not None:
                calls.append(_name)
            return "No events found."

        tool_registry.register(name, "d", handler, {"type": "object", "properties": {}})


class TestCallPromptFollowsOfferedTools:
    @pytest.mark.parametrize("offer_websearch", [False, True])
    async def test_mcp_block_names_only_servers_whose_tools_are_offered(
        self, settings, session_manager, cost_tracker, tool_registry, offer_websearch
    ):
        from pincer.voice.in_call_tools import bind_gate

        settings.voice_turn_model = ""
        _register(tool_registry, "google__list_events", "websearch__search", "weather__now")
        provider = ScriptedProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        agent.mcp_manager = _MCPStub({"websearch": ["websearch__search"], "weather": ["weather__now"]})
        allowed = {"google__list_events"} | ({"websearch__search"} if offer_websearch else set())

        with bind_gate(_call_gate(settings, allowed)):
            await _collect(agent, user_id="u1", channel="voice", text="Benzinpreis in Bünde?")

        system = provider.calls[0]["system"]
        offered = ["google__list_events"] + (["websearch__search"] if offer_websearch else [])
        assert [t["name"] for t in provider.calls[0]["tools"]] == offered
        assert "weather" not in system
        assert ("websearch (1 tool(s))" in system) is offer_websearch
        assert ("websearch server notes" in system) is offer_websearch

    async def test_no_tools_offered_means_no_tool_hints(self, settings, session_manager, cost_tracker, tool_registry):
        from pincer.voice.in_call_tools import bind_gate

        settings.voice_turn_model = ""
        _register(tool_registry, "google__list_events", "websearch__search")
        provider = ScriptedProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        agent.mcp_manager = _MCPStub({"websearch": ["websearch__search"]})

        with bind_gate(_call_gate(settings, set())):
            await _collect(agent, user_id="u1", channel="voice", text="Hallo")

        call = provider.calls[0]
        assert call["tools"] is None
        assert "Connected MCP servers" not in call["system"]
        assert "Google Workspace Tool Chains" not in call["system"]

    async def test_chat_turn_lists_the_servers_of_its_offered_tools(
        self, settings, session_manager, cost_tracker, tool_registry, mock_llm
    ):
        _register(tool_registry, "websearch__search", "weather__now")
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
        agent.mcp_manager = _MCPStub({"websearch": ["websearch__search"], "weather": ["weather__now"]})

        await agent.handle_message("u1", "telegram", "Was gibt es Neues?")

        system = mock_llm.complete.call_args.kwargs["system"]
        assert "websearch (1 tool(s))" in system
        assert "weather (1 tool(s))" in system


def _list_events_call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="google__list_events", arguments={"time_min": "2026-11-01", "max_results": 5})


class TestPartialServerAndGoogleHints:
    async def test_a_partly_offered_server_shows_only_the_offered_count(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        from pincer.voice.in_call_tools import bind_gate

        settings.voice_turn_model = ""
        google = ["google__list_events", "google__check_freebusy", "google__search_messages", "google__get_attachment"]
        _register(tool_registry, *google)
        provider = ScriptedProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        agent.mcp_manager = _MCPStub({"google": google})

        with bind_gate(_call_gate(settings, {"google__list_events", "google__check_freebusy"})):
            await _collect(agent, user_id="u1", channel="voice", text="Wann haben Sie Zeit?")

        system = provider.calls[0]["system"]
        assert "google (2 tool(s))" in system
        assert "google server notes" not in system
        assert "Google Workspace Tool Chains" not in system
        assert "google__search_messages" not in system

    async def test_google_hints_follow_the_tools_they_describe(
        self, settings, session_manager, cost_tracker, tool_registry, mock_llm
    ):
        _register(tool_registry, "google__search_messages", "google__get_attachment")
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

        await agent.handle_message("u1", "telegram", "Anhang speichern")

        assert "Google Workspace Tool Chains" in mock_llm.complete.call_args.kwargs["system"]

    async def test_without_a_gate_the_prompt_follows_the_voice_filter(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        settings.voice_turn_model = ""
        _register(tool_registry, "shell_exec", "weather__now")
        provider = ScriptedProvider([_text_turn("Gerne.")])
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        agent.mcp_manager = _MCPStub({"shell": ["shell_exec"], "weather": ["weather__now"]})

        await _collect(agent, user_id="u1", channel="voice", text="Hallo")

        names = [t["name"] for t in provider.calls[0]["tools"]]
        system = provider.calls[0]["system"]
        assert "shell_exec" not in names
        assert "shell (" not in system
        assert "weather (1 tool(s))" in system


class TestRepeatedToolCalls:
    async def test_a_repeated_call_is_not_run_again(self, settings, session_manager, cost_tracker, tool_registry):
        from pincer.voice.in_call_tools import bind_gate

        settings.voice_turn_model = ""
        calls: list[str] = []
        _register(tool_registry, "google__list_events", calls=calls)
        repeat = ToolCall(id="tc2", name="google__list_events", arguments={"max_results": 5, "time_min": "2026-11-01"})
        provider = ScriptedProvider(
            [
                _text_turn(tool_calls=[_list_events_call("tc1")]),
                _text_turn(tool_calls=[repeat]),
                _text_turn("Das kann ich während des Anrufs leider nicht nachschlagen."),
            ]
        )
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)
        gate = _call_gate(settings, {"google__list_events"})

        with bind_gate(gate):
            chunks = await _collect(agent, user_id="u1", channel="voice", text="Wo ist Benzin am günstigsten?")

        assert calls == ["google__list_events"]
        tool_events = [c.content for c in chunks if c.type == StreamEventType.TOOL_START]
        assert tool_events == ["google__list_events"]
        last_result = provider.calls[2]["messages"][-1]
        assert last_result.role == MessageRole.TOOL_RESULT
        assert last_result.tool_call_id == "tc2"
        assert last_result.content.startswith("[TOOL RESULT: ")
        assert "denselben Angaben" in last_result.content
        assert chunks[-1].content == "Das kann ich während des Anrufs leider nicht nachschlagen."

    async def test_the_turn_ends_after_repeated_duplicates(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        from pincer.voice.in_call_tools import bind_gate
        from pincer.voice.prompts import de as de_pack

        settings.voice_turn_model = ""
        calls: list[str] = []
        _register(tool_registry, "google__list_events", calls=calls)
        provider = ScriptedProvider(
            [
                _text_turn("Einen Moment. ", tool_calls=[_list_events_call("tc1")]),
                _text_turn(tool_calls=[_list_events_call("tc2")]),
                _text_turn(tool_calls=[_list_events_call("tc3")]),
                _text_turn("never reached"),
            ]
        )
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        with bind_gate(_call_gate(settings, {"google__list_events"})):
            chunks = await _collect(agent, user_id="u1", channel="voice", text="Benzinpreis?")

        assert calls == ["google__list_events"]
        assert len(provider.calls) == 3
        assert chunks[-1].type == StreamEventType.DONE
        assert chunks[-1].content == f"Einen Moment. {de_pack.TOOL_NO_PROGRESS}"
        session = await session_manager.get_or_create("u1", "voice")
        assert [m.tool_call_id for m in session.messages if m.role == MessageRole.TOOL_RESULT] == ["tc1", "tc2", "tc3"]

    async def test_the_blocking_call_path_ends_after_repeated_duplicates(
        self, settings, session_manager, cost_tracker, tool_registry, mock_llm
    ):
        from pincer.voice.in_call_tools import bind_gate
        from pincer.voice.prompts import de as de_pack

        calls: list[str] = []
        _register(tool_registry, "google__list_events", calls=calls)
        mock_llm.complete.side_effect = [
            LLMResponse(
                content="", tool_calls=[_list_events_call(f"tc{i}")], model="m", input_tokens=1, output_tokens=1
            )
            for i in range(3)
        ] + [LLMResponse(content="never reached", model="m", input_tokens=1, output_tokens=1)]
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

        with bind_gate(_call_gate(settings, {"google__list_events"})):
            result = await agent.handle_message("u1", "voice", "Benzinpreis?", session_key="voice:CA300")

        assert calls == ["google__list_events"]
        assert mock_llm.complete.await_count == 3
        assert result.text == de_pack.TOOL_NO_PROGRESS

    async def test_a_batch_with_a_new_call_goes_back_to_the_model(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        """At the repeat limit, a batch that also ran a new call is not cut short: the model gets its result."""
        from pincer.voice.in_call_tools import bind_gate

        settings.voice_turn_model = ""
        calls: list[str] = []
        _register(tool_registry, "google__list_events", "websearch__search", calls=calls)
        search = ToolCall(id="tc3", name="websearch__search", arguments={"query": "Benzin Bünde"})
        provider = ScriptedProvider(
            [
                _text_turn(tool_calls=[_list_events_call("tc1")]),
                _text_turn(tool_calls=[_list_events_call("tc2")]),
                _text_turn(tool_calls=[_list_events_call("tc4"), search]),
                _text_turn("E10 kostet dort 1,62 Euro."),
            ]
        )
        agent = Agent(settings, provider, session_manager, cost_tracker, tool_registry)

        with bind_gate(_call_gate(settings, {"google__list_events", "websearch__search"})):
            chunks = await _collect(agent, user_id="u1", channel="voice", text="Benzinpreis?")

        assert calls == ["google__list_events", "websearch__search"]
        assert len(provider.calls) == 4
        assert chunks[-1].content == "E10 kostet dort 1,62 Euro."

    async def test_a_repeat_of_a_failed_call_stays_an_error(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        from pincer.core.agent import _RepeatedCallGuard
        from pincer.llm.base import ToolResult

        guard = _RepeatedCallGuard(_call_gate(settings, {"google__list_events"}))
        first = _list_events_call("tc1")
        guard.record(first, ToolResult(tool_call_id="tc1", content="[TOOL DENIED: tool_error]", is_error=True))
        repeat = _list_events_call("tc2")
        assert guard.is_repeat(repeat)
        assert guard.repeat_result(repeat).is_error

    async def test_a_repeated_create_matches_despite_the_gate_idempotency_key(
        self, settings, session_manager, cost_tracker, tool_registry
    ):
        """The gate adds an idempotency key to the create's arguments (#299);
        the model's identical retry, without it, is still a repeat."""
        from pincer.core.agent import _RepeatedCallGuard
        from pincer.llm.base import ToolCall, ToolResult

        gate = _call_gate(settings, {"google__create_event"})
        guard = _RepeatedCallGuard(gate)
        args = {"summary": "Arzt", "start": "2026-11-02T12:00:00+01:00", "end": "2026-11-02T12:30:00+01:00"}
        first = ToolCall(id="tc1", name="google__create_event", arguments=dict(args))

        async def _run() -> tuple[str, bool]:
            return "Event created: 'Arzt'", False

        await gate.run(first.name, first.arguments, _run, trusted_key=False)
        assert "idempotency_key" in first.arguments
        guard.record(first, ToolResult(tool_call_id="tc1", content="ok", is_error=False))
        assert guard.is_repeat(ToolCall(id="tc2", name="google__create_event", arguments=dict(args)))

    async def test_chat_turns_are_not_guarded(self, settings, session_manager, cost_tracker, tool_registry, mock_llm):
        calls: list[str] = []
        _register(tool_registry, "google__list_events", calls=calls)
        mock_llm.complete.side_effect = [
            LLMResponse(
                content="", tool_calls=[_list_events_call(f"tc{i}")], model="m", input_tokens=1, output_tokens=1
            )
            for i in range(2)
        ] + [LLMResponse(content="done", model="m", input_tokens=1, output_tokens=1)]
        agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

        await agent.handle_message("u1", "telegram", "Termine?")

        assert calls == ["google__list_events", "google__list_events"]
