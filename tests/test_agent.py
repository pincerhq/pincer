"""Tests for the ReAct agent loop."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.core.agent import (
    _SESSION_CLEARED_TEXT,
    Agent,
    AgentResponse,
    StreamEventType,
    _is_tool_pair_error,
    _sanitize_tool_pairs,
)
from pincer.core.session import SessionManager
from pincer.exceptions import BudgetExceededError, LLMError
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.memory.mcp import MCPMemoryBackend
from pincer.memory.summarizer import Summarizer
from pincer.tools.builtin import files
from pincer.tools.registry import ToolRegistry

_DONE = LLMResponse(content="done", model="test", input_tokens=10, output_tokens=5, stop_reason="end_turn")
_OPENAI_UNPAIRED = (
    "openai API error: 400 An assistant message with 'tool_calls' must be followed by tool messages "
    "responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: late"
)
_ANTHROPIC_UNPAIRED = (
    "Anthropic API error: 400 messages.1: `tool_use` ids were found without `tool_result` blocks immediately after"
)
_ANTHROPIC_NO_TOOLS = (
    "Anthropic API error: 400 Requests which include `tool_use` or `tool_result` blocks must define tools."
)


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


def _mcp_memory_failing_store() -> MCPMemoryBackend:
    """An MCP memory backend whose memory_store returns a fastmcp tool error."""

    async def call_tool(name: str, args: dict) -> MagicMock:
        if name == "memory_store":
            err = [MagicMock(text="Error calling tool 'memory_store': embedding failed")]
            return MagicMock(content=err, isError=True, structuredContent=None)
        return MagicMock(content=[], isError=False, structuredContent=None)

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    manager = AsyncMock()
    manager.get_session.return_value = session
    backend = MCPMemoryBackend(server_name="memory-test")
    backend.set_mcp_manager(manager)
    return backend


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_summarization_memory_error_does_not_fail_the_turn(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, caplog, streaming
):
    """A memory_store tool error during summarization is logged; the user still gets a reply."""
    memory = _mcp_memory_failing_store()
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )

    async def turn(text: str) -> None:
        if not streaming:
            result = await agent.handle_message("user1", "test", text)
            assert "Hello! I'm Pincer." in result.text
            return
        chunks = [c async for c in agent.handle_message_stream(user_id="user1", channel="test", text=text)]
        assert chunks[-1].type == StreamEventType.DONE

    await turn("first")
    with caplog.at_level(logging.WARNING, logger="pincer.core.agent"):
        await turn("second")  # 3 non-system messages >= threshold → summarize

    warnings = [r for r in caplog.records if r.getMessage() == "Session summarization failed"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def _unpaired_call_ids(messages: list[LLMMessage]) -> set[str]:
    """IDs of assistant tool calls with no matching tool result."""
    result_ids = {m.tool_call_id for m in messages if m.role == MessageRole.TOOL_RESULT}
    return {tc.id for m in messages if m.role == MessageRole.ASSISTANT for tc in m.tool_calls} - result_ids


def _recording_complete(
    sent: list[list[LLMMessage]], *responses: LLMResponse | Exception
) -> Callable[..., Awaitable[LLMResponse]]:
    """A complete() side effect that snapshots the history each call was sent."""
    queue = list(responses)

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        sent.append(list(messages))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return complete


async def _run_turn(agent: Agent, text: str, *, streaming: bool) -> str:
    """One turn through handle_message or handle_message_stream; returns the reply."""
    if not streaming:
        return (await agent.handle_message("user1", "test", text)).text
    chunks = [c async for c in agent.handle_message_stream(user_id="user1", channel="test", text=text)]
    return "".join(c.content for c in chunks if c.type == StreamEventType.DONE)


def _orphaned_call(call_id: str = "late") -> LLMMessage:
    return LLMMessage(
        role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id=call_id, name="greet", arguments={})]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "tool_calls"),
    [
        # One call answered, one not: the message is rewritten, not dropped.
        ("", [ToolCall(id="ok", name="greet", arguments={}), ToolCall(id="orphan", name="greet", arguments={})]),
        # Every call orphaned, but the text survives as a plain message.
        ("Let me check.", [ToolCall(id="orphan", name="greet", arguments={})]),
    ],
    ids=["partially-orphaned", "orphaned-with-text"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_orphaned_tool_calls_never_reach_the_llm(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, content, tool_calls, streaming
):
    """Sanitizing that rewrites a message keeps the history's length, which
    used to hide the repair — the orphaned calls were sent and OpenAI 400'd."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls),
    ]
    if any(tc.id == "ok" for tc in tool_calls):
        session.messages.append(LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"))

    sent: list[list[LLMMessage]] = []
    mock_llm.complete.side_effect = _recording_complete(sent, _DONE)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _run_turn(agent, "again", streaming=streaming)

    assert len(sent) == 1
    assert _unpaired_call_ids(sent[0]) == set()
    assert _unpaired_call_ids(session.messages) == set()


@pytest.mark.asyncio
async def test_repaired_history_is_persisted(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path
):
    """The repair is saved, not just applied to the cached session: the
    turn fails before anything else would write the session out."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [LLMMessage(role=MessageRole.USER, content="hi"), _orphaned_call()]
    mock_llm.complete.side_effect = LLMError("openai API error: 500 server error")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "again")

    # A second manager on the same database reads the stored row, not the cache.
    store = SessionManager(Path(tmp_path) / "test.db")
    await store.initialize()
    try:
        stored = await store.get_or_create("user1", "test")
        assert [m.content for m in stored.messages] == ["hi", "again"]
        assert _unpaired_call_ids(stored.messages) == set()
    finally:
        await store.close()


def test_sanitize_requires_results_directly_after_their_call():
    """OpenAI rejects a result that isn't in the run right after its call,
    so a result separated by another message counts as orphaned, as does a
    second result for the same call."""
    messages = [
        _orphaned_call(),
        LLMMessage(role=MessageRole.USER, content="interrupting"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="late"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="ok", name="greet", arguments={})]),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello again!", tool_call_id="ok"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [(m.role, m.tool_call_id) for m in clean] == [
        (MessageRole.USER, None),
        (MessageRole.ASSISTANT, None),
        (MessageRole.TOOL_RESULT, "ok"),
    ]
    assert clean[2].content == "Hello!"


def test_sanitize_pairs_a_repeated_call_id_once():
    """A compatible server can return the same (or an empty) id for two calls;
    only the first call and its first result survive, so the output is clean."""
    messages = [
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="x", name="greet", arguments={}), ToolCall(id="x", name="greet", arguments={})],
        ),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="ra", tool_call_id="x"),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="rb", tool_call_id="x"),
    ]

    clean = _sanitize_tool_pairs(messages)

    assert [tc.id for tc in clean[0].tool_calls] == ["x"]
    assert [m.content for m in clean[1:]] == ["ra"]
    assert _sanitize_tool_pairs(clean) is clean


def test_sanitize_returns_a_clean_history_unchanged():
    messages = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="ok", name="greet", arguments={})]),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Hello!", tool_call_id="ok"),
    ]
    assert _sanitize_tool_pairs(messages) is messages


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_OPENAI_UNPAIRED, True),
        (
            "openai API error: 400 Invalid parameter: messages with role 'tool' must be a response to a "
            "preceeding message with 'tool_calls'.",
            True,
        ),
        (_ANTHROPIC_UNPAIRED, True),
        (_ANTHROPIC_NO_TOOLS, True),
        ("openai API error: 400 Invalid 'messages[2].tool_calls[0].id': string too long", False),
        ("openai API error: 400 Invalid 'tool_call_id': expected a string", False),
    ],
    ids=[
        "openai-unanswered-call",
        "openai-stray-result",
        "anthropic-unanswered-call",
        "anthropic-no-tools",
        "openai-bad-id",
        "openai-bad-tool-call-id",
    ],
)
def test_is_tool_pair_error(error, expected):
    assert _is_tool_pair_error(LLMError(error)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_tool_pair_error_sanitizes_and_retries(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """A history that turns bad between the proactive pass and the call (a
    concurrent turn writing to the session) is repaired once the provider
    rejects it, and the retry no longer carries the orphan."""
    sent: list[list[LLMMessage]] = []

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        sent.append(list(messages))
        if len(sent) == 1:
            messages.append(_orphaned_call())
            raise LLMError(_OPENAI_UNPAIRED)
        return _DONE

    mock_llm.complete.side_effect = complete
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await _run_turn(agent, "hi", streaming=streaming)

    assert len(sent) == 2
    assert _unpaired_call_ids(sent[1]) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [_OPENAI_UNPAIRED, _ANTHROPIC_UNPAIRED, _ANTHROPIC_NO_TOOLS],
    ids=["openai-unanswered-call", "anthropic-unanswered-call", "anthropic-no-tools"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_unrepairable_tool_pair_error_clears_the_session_without_retrying(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, error, streaming
):
    """When stripping changes nothing, re-sending would fail the same way on
    this and every later turn (e.g. Anthropic's "must define tools" for a
    history with tool pairs sent with no tools), so the session is cleared."""
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="earlier"),
        LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"),
    ]
    mock_llm.complete.side_effect = LLMError(error)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    reply = await _run_turn(agent, "hi", streaming=streaming)

    assert reply.startswith(_SESSION_CLEARED_TEXT)
    assert mock_llm.complete.call_count == 1
    assert "earlier" not in [m.content for m in session.messages]


@pytest.mark.asyncio
async def test_other_llm_errors_raise_and_keep_the_history(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    session = await session_manager.get_or_create("user1", "test")
    session.messages = [
        LLMMessage(role=MessageRole.USER, content="earlier"),
        LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"),
    ]
    mock_llm.complete.side_effect = LLMError("openai API error: 400 Invalid 'tool_call_id': expected a string")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError):
        await agent.handle_message("user1", "test", "hi")

    assert mock_llm.complete.call_count == 1
    assert [m.content for m in session.messages] == ["earlier", "earlier reply", "hi"]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_turn_without_memory_neither_reads_nor_writes_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """memory=False (a call summary, a knowledge-base question): the user's
    long-term memory stays out of the prompt, and nothing of the turn is
    stored there, not even as a session summary."""
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="the user's private fact")]
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    summarizer.maybe_summarize = AsyncMock(wraps=summarizer.maybe_summarize)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )

    for text in ("summarize call one", "summarize call two"):
        if streaming:
            chunks = [c async for c in agent.handle_message_stream("user1", "test", text, memory=False)]
            assert chunks[-1].type == StreamEventType.DONE
        else:
            result = await agent.handle_message("user1", "test", text, memory=False)
            assert "Hello! I'm Pincer." in result.text

    memory.search_text.assert_not_awaited()
    memory.store_memory.assert_not_awaited()
    memory.add_profile.assert_not_awaited()
    summarizer.maybe_summarize.assert_not_awaited()
    for call in mock_llm.complete.call_args_list:
        assert "the user's private fact" not in call.kwargs["system"]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_conversation_without_memory_stays_without_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, streaming
):
    """A memory-free conversation continued with memory=True is still
    memory-free: its exchanges never reach long-term memory, summary included."""
    memory = AsyncMock()
    summarizer = Summarizer(mock_llm, memory, session_manager, threshold=2)
    summarizer.maybe_summarize = AsyncMock(wraps=summarizer.maybe_summarize)
    agent = Agent(
        settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory, summarizer=summarizer
    )
    key = "test:user1:summary"

    for text, with_memory in (("summarize the call", False), ("and the action items?", True)):
        if streaming:
            stream = agent.handle_message_stream("user1", "test", text, session_key=key, memory=with_memory)
            chunks = [c async for c in stream]
            assert chunks[-1].type == StreamEventType.DONE
        else:
            await agent.handle_message("user1", "test", text, session_key=key, memory=with_memory)

    memory.search_text.assert_not_awaited()
    memory.store_memory.assert_not_awaited()
    summarizer.maybe_summarize.assert_not_awaited()


async def test_a_turn_with_memory_still_reads_and_writes_it(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The default is unchanged: memory is searched and the exchange stored."""
    memory = AsyncMock()
    memory.search_text.return_value = [SimpleNamespace(content="the user's private fact")]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await agent.handle_message("user1", "test", "hello")

    memory.search_text.assert_awaited()
    memory.store_memory.assert_awaited_once()
    assert "the user's private fact" in mock_llm.complete.call_args.kwargs["system"]
