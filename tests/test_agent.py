"""Tests for the ReAct agent loop."""

import pytest

from pincer.core.agent import Agent, AgentResponse
from pincer.llm.base import LLMResponse, ToolCall


async def _approval_capture(tool_name, arguments, user_id, channel):
    """Capture user_id passed to approval callback for assertion."""
    _approval_capture.last_user_id = user_id  # type: ignore[attr-defined]
    _approval_capture.last_channel = channel  # type: ignore[attr-defined]
    return True


@pytest.mark.asyncio
async def test_simple_response(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user1", "test", "Hello!")
    assert isinstance(result, AgentResponse)
    assert result.text == "Hello! I'm Pincer."
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
    assert result.text == "Sorry, that tool isn't available."


@pytest.mark.asyncio
async def test_approval_uses_channel_user_id(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """When channel_user_id is passed, approval callback receives it (not pincer_user_id)."""

    async def _dummy_tool():
        return "ok"

    tool_registry.register(
        name="needs_approval",
        description="Tool that requires approval",
        handler=_dummy_tool,
        parameters={"type": "object", "properties": {}},
        require_approval=True,
    )

    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="needs_approval", arguments={})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="Approved and done.",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(
        settings,
        mock_llm,
        session_manager,
        cost_tracker,
        tool_registry,
        approval_callback=_approval_capture,
    )
    await agent.handle_message(
        user_id="usr_abc123",
        channel="telegram",
        text="Run needs_approval",
        pincer_user_id="usr_abc123",
        channel_user_id="12345",
    )
    assert getattr(_approval_capture, "last_user_id", None) == "12345"
    assert getattr(_approval_capture, "last_channel", None) == "telegram"
