"""Tests for the ReAct agent loop."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pincer.core.agent import Agent, AgentResponse
from pincer.exceptions import BudgetExceededError, LLMError
from pincer.llm.base import LLMResponse, ToolCall
from pincer.tools.builtin import files
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

    assert seen_contexts == [
        {"user_id": "123456789", "channel": "test", "pincer_user_id": "canonical-r_lutsiv", "channel_name": "test"}
    ]


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

    assert seen_contexts == [{"user_id": "user1", "channel": "test", "pincer_user_id": "user1", "channel_name": "test"}]


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

    assert seen_contexts == [
        {"user_id": "123456789", "channel": "test", "pincer_user_id": "canonical-r_lutsiv", "channel_name": "test"}
    ]


@pytest.mark.asyncio
async def test_run_headless_no_session_writes(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    """run_headless must not touch SessionManager at all (proactive/scheduled runs are session-free)."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with (
        patch.object(session_manager, "get_or_create", wraps=session_manager.get_or_create) as get_or_create_spy,
        patch.object(session_manager, "add_message", wraps=session_manager.add_message) as add_message_spy,
    ):
        result = await agent.run_headless("Say hi", user_id="usr1", channel="scheduled")

    assert get_or_create_spy.await_count == 0
    assert add_message_spy.await_count == 0
    assert "Hello! I'm Pincer." in result


@pytest.mark.asyncio
async def test_run_headless_tool_call_round_trip(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="The greeting is: Hello, World!",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.run_headless("Greet the world", user_id="usr1", channel="scheduled")

    assert "Hello, World!" in result
    assert mock_llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_run_headless_skips_approval(settings, mock_llm, session_manager, cost_tracker):
    """Tool calls in run_headless run with skip_approval=True — no live user to approve anything."""
    registry = ToolRegistry()

    async def risky() -> str:
        return "done"

    registry.register(name="risky", description="risky", handler=risky, require_approval=True)

    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="risky", arguments={})],
            model="test",
            input_tokens=10,
            output_tokens=5,
            stop_reason="tool_use",
        ),
        LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn"),
    ]

    approval_callback = AsyncMock(return_value=True)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry, approval_callback=approval_callback)

    result = await agent.run_headless("do the risky thing", user_id="usr1", channel="scheduled")

    approval_callback.assert_not_awaited()
    assert "done" in result


@pytest.mark.asyncio
async def test_run_headless_allowed_tools_filters_schema(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """allowed_tools restricts which tool schemas reach the LLM, not just which calls are permitted."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await agent.run_headless("hi", user_id="usr1", channel="scheduled", allowed_tools=["nonexistent_tool"])

    call_kwargs = mock_llm.complete.call_args.kwargs
    assert call_kwargs["tools"] is None


@pytest.mark.asyncio
async def test_run_headless_allowed_tools_keeps_matching_tool(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await agent.run_headless("hi", user_id="usr1", channel="scheduled", allowed_tools=["greet"])

    call_kwargs = mock_llm.complete.call_args.kwargs
    assert [t["name"] for t in call_kwargs["tools"]] == ["greet"]


@pytest.mark.asyncio
async def test_run_headless_budget_exceeded_during_llm_call(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    mock_llm.complete.side_effect = BudgetExceededError(spent=100.0, limit=100.0)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="usr1", channel="scheduled")

    assert "budget limit reached" in result.lower()
    assert "$100.00" in result


@pytest.mark.asyncio
async def test_run_headless_llm_error(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    mock_llm.complete.side_effect = LLMError("connection reset")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="usr1", channel="scheduled")

    assert "llm error" in result.lower()


@pytest.mark.asyncio
async def test_run_headless_budget_exceeded_during_cost_record(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with patch.object(cost_tracker, "record", side_effect=BudgetExceededError(spent=60.0, limit=100.0)):
        result = await agent.run_headless("hi", user_id="usr1", channel="scheduled")

    assert "budget limit reached" in result.lower()
    assert "$60.00" in result
    assert "$100.00" in result


@pytest.mark.asyncio
async def test_run_headless_circuit_breaker_after_repeated_tool_errors(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A tool call that keeps failing (unknown tool name) trips the circuit breaker after 3 iterations."""
    mock_llm.complete.return_value = LLMResponse(
        content="still trying",
        tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
        model="test",
        input_tokens=10,
        output_tokens=5,
        stop_reason="tool_use",
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("do the thing", user_id="usr1", channel="scheduled")

    assert result == "still trying"
    assert mock_llm.complete.call_count == 3


@pytest.mark.asyncio
async def test_run_headless_exhausts_iterations_returns_last_response(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """With max_iterations reached and no final no-tool-call response, the last response content is returned."""
    mock_llm.complete.return_value = LLMResponse(
        content="partial thoughts",
        tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
        model="test",
        input_tokens=10,
        output_tokens=5,
        stop_reason="tool_use",
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    result = await agent.run_headless("hi", user_id="usr1", channel="scheduled", max_iterations=1)

    assert result == "partial thoughts"
    assert mock_llm.complete.call_count == 1


@pytest.mark.asyncio
async def test_execute_tool_sandbox_denied_logs_warning_not_error(
    settings, mock_llm, session_manager, cost_tracker, caplog, tmp_path, monkeypatch
):
    """An out-of-workspace file_read (issue #218) is expected policy, not a crash — no ERROR, no traceback.

    Uses the real file_read so the whole file_read → _sandbox_path → confine_path chain is exercised.
    """
    monkeypatch.setattr(files, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    registry = ToolRegistry()
    registry.register(
        name="file_read",
        description="Read a file",
        handler=files.file_read,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    tool_call = ToolCall(id="tc1", name="file_read", arguments={"path": "/etc/passwd"})

    with caplog.at_level(logging.INFO):
        result = await agent._execute_tool(tool_call, "user1", "test")

    assert result.is_error is True
    assert "outside workspace" in result.content
    assert "All file operations are sandboxed." in result.content

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is None


@pytest.mark.asyncio
async def test_execute_tool_genuine_error_still_logs_error_with_traceback(
    settings, mock_llm, session_manager, cost_tracker, caplog
):
    """Non-sandbox tool failures are unaffected — still ERROR level, with a traceback."""
    registry = ToolRegistry()

    async def boom() -> str:
        raise RuntimeError("something actually broke")

    registry.register(
        name="boom",
        description="Always fails",
        handler=boom,
        parameters={"type": "object", "properties": {}},
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    tool_call = ToolCall(id="tc1", name="boom", arguments={})

    with caplog.at_level(logging.INFO):
        result = await agent._execute_tool(tool_call, "user1", "test")

    assert result.is_error is True
    assert "RuntimeError" in result.content

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None


async def test_unavailable_memories_warn_and_still_reply(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """A failing memory backend costs the user their memories, not their reply — and says so at WARNING."""
    memory = AsyncMock()
    memory.search_text.side_effect = ConnectionError("memory backend down")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        result = await agent.handle_message("user1", "test", "Hello!")

    assert "Hello! I'm Pincer." in result.text
    memory.search_text.assert_awaited()
    system_prompt = mock_llm.complete.call_args.kwargs["system"]
    assert "[Relevant memories about this user]" not in system_prompt

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage() == "Failed to fetch memories for prompt"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def _outage_log(caplog: pytest.LogCaptureFixture, failure: str, recovery: str) -> list[tuple[str, bool]]:
    """(level, has traceback) of each failure/recovery record, in order."""
    return [
        (record.levelname, record.exc_info is not None)
        for record in caplog.records
        if record.getMessage() in {failure, recovery}
    ]


async def test_an_unavailable_memory_backend_warns_once_per_outage(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """Once per message would flood the log for the length of an outage."""
    memory = AsyncMock()
    memory.search_text.side_effect = [
        ConnectionError("down"),
        ConnectionError("still down"),
        [],
        ConnectionError("again"),
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(4):
            await agent.handle_message("user1", "test", "Hello!")

    assert memory.search_text.await_count == 4
    assert _outage_log(caplog, "Failed to fetch memories for prompt", "Fetching memories for prompt recovered") == [
        ("WARNING", True),
        ("DEBUG", True),
        ("INFO", False),
        ("WARNING", True),
    ]


async def test_an_unavailable_mcp_server_list_warns_once_per_outage(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    agent.mcp_manager = AsyncMock()
    agent.mcp_manager.list_servers.side_effect = [
        RuntimeError("down"),
        RuntimeError("still down"),
        [],
        RuntimeError("again"),
    ]

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(4):
            await agent.handle_message("user1", "test", "Hello!")

    assert agent.mcp_manager.list_servers.await_count == 4
    assert _outage_log(
        caplog, "Failed to fetch MCP server list for prompt", "Fetching the MCP server list for prompt recovered"
    ) == [("WARNING", True), ("DEBUG", True), ("INFO", False), ("WARNING", True)]


class _Unprintable:
    @property
    def content(self) -> str:
        raise RuntimeError("cannot render")


async def test_a_memory_that_fails_to_render_warns_once_and_never_recovers(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    """The fetch succeeds, then formatting it fails: that is one outage, not a
    recovery followed by a new failure on every message."""
    memory = AsyncMock()
    memory.search_text.return_value = [_Unprintable()]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(3):
            await agent.handle_message("user1", "test", "Hello!")

    assert _outage_log(caplog, "Failed to fetch memories for prompt", "Fetching memories for prompt recovered") == [
        ("WARNING", True),
        ("DEBUG", True),
        ("DEBUG", True),
    ]


async def test_an_mcp_server_list_that_fails_to_render_warns_once_and_never_recovers(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog
):
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    agent.mcp_manager = AsyncMock()
    agent.mcp_manager.list_servers.return_value = [{"connected": True}]  # no "name": formatting raises

    with caplog.at_level(logging.DEBUG, logger="pincer.core.agent"):
        for _ in range(3):
            await agent.handle_message("user1", "test", "Hello!")

    assert _outage_log(
        caplog, "Failed to fetch MCP server list for prompt", "Fetching the MCP server list for prompt recovered"
    ) == [("WARNING", True), ("DEBUG", True), ("DEBUG", True)]
