"""Tests for the ReAct agent loop."""

import pytest

from pincer.core.agent import Agent, AgentResponse
from pincer.llm.base import LLMResponse, ToolCall
from pincer.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_simple_response(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Hello!")
    assert isinstance(result, AgentResponse)
    assert "Hello! I'm Pincer." in result.text
    assert result.tool_calls_made == 0


@pytest.mark.asyncio
async def test_tool_call_loop(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    # First call: LLM wants to use a tool
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        # Second call: LLM produces final text after seeing tool result
        LLMResponse(
            content="The greeting is: Hello, World!",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Greet the world")

    assert "Hello, World!" in result.text
    assert result.tool_calls_made == 1
    assert mock_llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_tool_not_found(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="Sorry, that tool isn't available.",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Use nonexistent tool")
    # Agent should handle gracefully — the LLM gets an error and responds
    assert "Sorry, that tool isn't available." in result.text


def _context_capturing_registry() -> tuple[ToolRegistry, list[dict]]:
    """A registry with one tool that records the `context` dict it was called with.

    Mirrors channel-bound tools like send_file/send_image, which read
    context["user_id"] expecting the channel-native ID (issue #162).
    """
    registry = ToolRegistry()
    seen_contexts: list[dict] = []

    async def whoami(context: dict | None = None) -> str:
        seen_contexts.append(context or {})
        return "ok"

    registry.register(
        name="whoami",
        description="Records the context it receives",
        handler=whoami,
        parameters={"type": "object", "properties": {}},
    )
    return registry, seen_contexts


@pytest.mark.asyncio
async def test_tool_context_prefers_channel_user_id(settings, mock_llm, session_manager, cost_tracker):
    """Channel-bound tools must see the channel-native ID, not the canonical user_id (issue #162)."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    await agent.handle_message(
        user_id="canonical-r_lutsiv",
        channel="test",
        text="who am i",
        channel_user_id="123456789",
    )

    assert seen_contexts == [{"user_id": "123456789", "channel": "test"}]


@pytest.mark.asyncio
async def test_tool_context_falls_back_to_user_id_when_no_channel_user_id(
    settings, mock_llm, session_manager, cost_tracker
):
    """Without a channel_user_id (e.g. CLI/local chat), the canonical user_id still works."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    await agent.handle_message(user_id="user1", channel="test", text="who am i")

    assert seen_contexts == [{"user_id": "user1", "channel": "test"}]


@pytest.mark.asyncio
async def test_stream_tool_context_prefers_channel_user_id(settings, mock_llm, session_manager, cost_tracker):
    """Same fix applied to the streaming path (handle_message_stream previously dropped channel_user_id)."""
    registry, seen_contexts = _context_capturing_registry()
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="whoami", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="done",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    async for _chunk in agent.handle_message_stream(
        user_id="canonical-r_lutsiv",
        channel="test",
        text="who am i",
        channel_user_id="123456789",
    ):
        pass

    assert seen_contexts == [{"user_id": "123456789", "channel": "test"}]
