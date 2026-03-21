"""
Integration tests for MCPClientManager + MCPToolBridge end-to-end.

Spins up real FastMCP echo servers via subprocess and verifies the full
lifecycle: connect → tool registration → tool calls → audit log → shutdown.

Skipped automatically if the mcp package is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path  # noqa: TC003

import pytest

pytestmark = pytest.mark.asyncio

mcp = pytest.importorskip("mcp", reason="mcp package not installed")


# ── Server script fixtures ────────────────────────────────────────────────────


def _write_echo_server(path: Path, name: str = "echo-test", extra_tool: str | None = None) -> str:
    """Write a minimal FastMCP echo server script and return its path."""
    extra = ""
    if extra_tool:
        extra = f"""

@mcp.tool()
def {extra_tool}(x: int) -> int:
    \"\"\"Return x doubled.\"\"\"
    return x * 2
"""
    script = path / f"{name.replace('-', '_')}_server.py"
    script.write_text(
        f"""\
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("{name}")


@mcp.tool()
def echo(message: str) -> str:
    \"\"\"Echo a message back.\"\"\"
    return f"echo: {{message}}"
{extra}

if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    )
    return str(script)


@pytest.fixture
def two_echo_servers(tmp_path):
    """Return configs for two separate FastMCP echo servers."""
    from pincer.mcp.config import MCPConfig, MCPServerConfig, MCPTransport

    script_a = _write_echo_server(tmp_path, "alpha")
    script_b = _write_echo_server(tmp_path, "beta", extra_tool="double")

    servers = [
        MCPServerConfig(
            name="alpha",
            transport=MCPTransport.STDIO,
            command=sys.executable,
            args=[script_a],
            sandbox=False,
            timeout_seconds=15,
            approval_required=["none"],
        ),
        MCPServerConfig(
            name="beta",
            transport=MCPTransport.STDIO,
            command=sys.executable,
            args=[script_b],
            sandbox=False,
            timeout_seconds=15,
            approval_required=["none"],
        ),
    ]
    return MCPConfig(servers=servers)


@pytest.fixture
def one_good_one_bad(tmp_path):
    """Return configs where one server is reachable and one is not."""
    from pincer.mcp.config import MCPConfig, MCPServerConfig, MCPTransport

    script_a = _write_echo_server(tmp_path, "good")

    servers = [
        MCPServerConfig(
            name="good",
            transport=MCPTransport.STDIO,
            command=sys.executable,
            args=[script_a],
            sandbox=False,
            timeout_seconds=15,
            approval_required=["none"],
        ),
        MCPServerConfig(
            name="bad",
            transport=MCPTransport.STDIO,
            command="nonexistent-command-that-does-not-exist",
            args=[],
            sandbox=False,
            timeout_seconds=5,
            approval_required=["none"],
        ),
    ]
    return MCPConfig(servers=servers)


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_manager_connects_multiple_servers(two_echo_servers) -> None:
    """Start manager with 2 servers — both connect, tools registered with prefixes."""
    from pincer.mcp.manager import MCPClientManager
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    manager = MCPClientManager(two_echo_servers, registry, audit_logger=None)

    results = await manager.start()
    try:
        assert results == {"alpha": True, "beta": True}
        tools = registry.list_tools()
        assert "alpha__echo" in tools
        assert "beta__echo" in tools
        assert "beta__double" in tools
    finally:
        await manager.stop()


async def test_manager_handles_one_server_failure(one_good_one_bad) -> None:
    """One unreachable server doesn't prevent the other from connecting."""
    from pincer.mcp.manager import MCPClientManager
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    manager = MCPClientManager(one_good_one_bad, registry, audit_logger=None)

    results = await manager.start()
    try:
        assert results["good"] is True
        assert results["bad"] is False
        # Good server's tools present; bad server's tools absent
        tools = registry.list_tools()
        assert "good__echo" in tools
        assert not any(t.startswith("bad__") for t in tools)
    finally:
        await manager.stop()


async def test_manager_tool_call_through_bridge(two_echo_servers) -> None:
    """Call a tool via the registry — flows bridge → session → MCP server → result."""
    from pincer.mcp.manager import MCPClientManager
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    manager = MCPClientManager(two_echo_servers, registry, audit_logger=None)

    await manager.start()
    try:
        result = await registry.execute("alpha__echo", {"message": "hello"})
        assert "echo: hello" in result

        result2 = await registry.execute("beta__double", {"x": 7})
        assert "14" in result2
    finally:
        await manager.stop()


async def test_manager_shutdown_cleans_everything(two_echo_servers) -> None:
    """After stop(), all sessions disconnected, all tools deregistered."""
    from pincer.mcp.manager import MCPClientManager
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    manager = MCPClientManager(two_echo_servers, registry, audit_logger=None)

    await manager.start()
    assert len(registry.list_tools()) > 0

    await manager.stop()

    assert registry.list_tools() == []
    assert manager._sessions == {}
    assert manager._bridges == {}
    assert manager._health_task is None


async def test_manager_audit_log_populated(two_echo_servers, tmp_path) -> None:
    """Audit log contains connect, tool_call, and disconnect events after full lifecycle."""
    from pincer.mcp.manager import MCPClientManager
    from pincer.security.audit import AuditLogger
    from pincer.tools.registry import ToolRegistry

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    registry = ToolRegistry()
    manager = MCPClientManager(two_echo_servers, registry, audit_logger=real_logger)

    await manager.start()
    await registry.execute("alpha__echo", {"message": "test"})
    await manager.stop()

    await real_logger._flush_pending()

    # Should have connect events for both servers
    alpha_events = await real_logger.query(user_id="mcp:alpha")
    beta_events = await real_logger.query(user_id="mcp:beta")
    assert len(alpha_events) >= 1
    assert len(beta_events) >= 1

    # Tool call event for alpha__echo
    tool_events = [e for e in alpha_events if e.get("tool") == "echo"]
    assert len(tool_events) >= 1

    await real_logger.shutdown()
