"""Tests for Agent + MCPClientManager integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _make_agent():
    """Build a minimal Agent with all deps mocked."""
    from pincer.core.agent import Agent

    settings = MagicMock()
    settings.system_prompt = "You are helpful."
    settings.max_tool_iterations = 5
    settings.default_provider = MagicMock(value="anthropic")
    settings.daily_budget_usd = 10.0
    settings.mcp_instructions_max_chars = 400

    llm = MagicMock()
    session_mgr = MagicMock()
    cost_tracker = MagicMock()
    cost_tracker.record = AsyncMock(return_value=0.001)
    tool_registry = MagicMock()
    tool_registry.list_tools = MagicMock(return_value=[])
    tool_registry.has_tools = False
    tool_registry.get_schemas = MagicMock(return_value=[])

    return Agent(
        settings=settings,
        llm=llm,
        session_manager=session_mgr,
        cost_tracker=cost_tracker,
        tool_registry=tool_registry,
    )


def _make_mcp_manager(connected: bool = True) -> MagicMock:
    manager = MagicMock()
    manager.list_servers = AsyncMock(
        return_value=[
            {"name": "github", "connected": connected, "tool_count": 5},
            {"name": "slack", "connected": connected, "tool_count": 3},
        ]
    )
    manager.total_tool_count = MagicMock(return_value=8 if connected else 0)
    return manager


# ── mcp_manager attribute ─────────────────────────────────────────────────────


def test_agent_mcp_manager_defaults_none():
    agent = _make_agent()
    assert agent.mcp_manager is None


def test_agent_mcp_manager_can_be_set():
    agent = _make_agent()
    manager = _make_mcp_manager()
    agent.mcp_manager = manager
    assert agent.mcp_manager is manager


# ── tool_registry property ────────────────────────────────────────────────────


def test_agent_tool_registry_property():
    agent = _make_agent()
    assert agent.tool_registry is agent._tools


# ── system prompt MCP section ─────────────────────────────────────────────────


async def test_build_system_prompt_no_mcp_manager():
    """Without mcp_manager, prompt is unmodified."""
    agent = _make_agent()
    prompt = await agent._build_system_prompt("user1", "hello")
    assert "[Connected MCP servers" not in prompt


async def test_build_system_prompt_with_mcp_manager_connected():
    """With connected MCP manager, prompt includes server list."""
    agent = _make_agent()
    agent.mcp_manager = _make_mcp_manager(connected=True)

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "[Connected MCP servers" in prompt
    assert "github" in prompt
    assert "slack" in prompt


async def test_build_system_prompt_mcp_no_connected_servers():
    """Prompt does not include MCP section when no servers are connected."""
    agent = _make_agent()
    manager = MagicMock()
    manager.list_servers = AsyncMock(
        return_value=[
            {"name": "down", "connected": False, "tool_count": 0},
        ]
    )
    agent.mcp_manager = manager

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "[Connected MCP servers" not in prompt


async def test_build_system_prompt_includes_server_instructions():
    """A connected server's instructions text appears under its name/tool_count line."""
    agent = _make_agent()
    manager = MagicMock()
    manager.list_servers = AsyncMock(
        return_value=[
            {
                "name": "doctr",
                "connected": True,
                "tool_count": 6,
                "instructions": "Use ocr_document_text for a quick plain-text read of an image or PDF.",
            },
        ]
    )
    agent.mcp_manager = manager

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "doctr (6 tool(s))" in prompt
    assert "Use ocr_document_text for a quick plain-text read of an image or PDF." in prompt


async def test_build_system_prompt_missing_instructions_key_is_backward_compatible():
    """Server dicts without an 'instructions' key (older shape) must not crash or add a stray line."""
    agent = _make_agent()
    agent.mcp_manager = _make_mcp_manager(connected=True)

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "github (5 tool(s))" in prompt
    assert "→" not in prompt


async def test_build_system_prompt_none_instructions_adds_no_line():
    """A server with instructions=None gets no follow-up line, no crash."""
    agent = _make_agent()
    manager = MagicMock()
    manager.list_servers = AsyncMock(
        return_value=[
            {"name": "quiet", "connected": True, "tool_count": 1, "instructions": None},
        ]
    )
    agent.mcp_manager = manager

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "quiet (1 tool(s))" in prompt
    assert "→" not in prompt


async def test_build_system_prompt_truncates_long_instructions():
    """Instructions longer than mcp_instructions_max_chars are truncated with an ellipsis."""
    agent = _make_agent()
    agent._settings.mcp_instructions_max_chars = 20
    manager = MagicMock()
    manager.list_servers = AsyncMock(
        return_value=[
            {
                "name": "verbose",
                "connected": True,
                "tool_count": 1,
                "instructions": "This is a very long instructions string that exceeds the configured cap.",
            },
        ]
    )
    agent.mcp_manager = manager

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "This is a very long" in prompt
    assert "…" in prompt
    assert "exceeds the configured cap" not in prompt


async def test_build_system_prompt_mcp_list_servers_error_is_silent():
    """list_servers() exception doesn't propagate — prompt returns without MCP section."""
    agent = _make_agent()
    manager = MagicMock()
    manager.list_servers = AsyncMock(side_effect=RuntimeError("boom"))
    agent.mcp_manager = manager

    prompt = await agent._build_system_prompt("user1", "hello")
    assert agent._settings.system_prompt in prompt
    assert "[Connected MCP servers" not in prompt


async def test_build_system_prompt_shows_tool_count():
    """Tool count appears in the server line."""
    agent = _make_agent()
    agent.mcp_manager = _make_mcp_manager(connected=True)

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "5 tool(s)" in prompt or "tool" in prompt


async def test_build_system_prompt_mcp_and_memory_both_present():
    """MCP section and memory section co-exist in the prompt."""
    from pincer.memory.store import MemoryStore

    agent = _make_agent()
    agent.mcp_manager = _make_mcp_manager(connected=True)

    mock_memory = MagicMock(spec=MemoryStore)
    mock_memory.search_text = AsyncMock(
        return_value=[
            MagicMock(content="User prefers brevity"),
        ]
    )
    agent._memory = mock_memory

    prompt = await agent._build_system_prompt("user1", "hello")
    assert "[Connected MCP servers" in prompt
    assert "User prefers brevity" in prompt
