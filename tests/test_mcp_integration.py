"""
End-to-end integration tests for MCPClientSession.

These tests spin up a real FastMCP server as a subprocess and verify that
MCPClientSession can connect, discover tools, call them, and disconnect.

Requires: uv pip install 'pincer-agent[mcp]'
Skipped automatically if the mcp package is not installed.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.asyncio

# Skip entire module if mcp is not installed
mcp = pytest.importorskip("mcp", reason="mcp package not installed")


@pytest.fixture
def echo_server_script(tmp_path):
    """Write a minimal FastMCP echo server to a temp file."""
    script = tmp_path / "echo_server.py"
    script.write_text(
        """\
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


@mcp.tool()
def echo(message: str) -> str:
    \"\"\"Echo a message back.\"\"\"
    return f"echo: {message}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    )
    return str(script)


@pytest.fixture
def slow_server_script(tmp_path):
    """Write a FastMCP server with a slow tool to a temp file."""
    script = tmp_path / "slow_server.py"
    script.write_text(
        """\
import time
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slow-test")


@mcp.tool()
def slow_op() -> str:
    \"\"\"Slow operation.\"\"\"
    time.sleep(30)
    return "done"


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    )
    return str(script)


# ── Non-sandboxed path ────────────────────────────────────────────────────────


async def test_connect_discover_call_disconnect(echo_server_script: str) -> None:
    """Happy path: connect → discover tools → call tool → disconnect."""
    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    config = MCPServerConfig(
        name="echo_test",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[echo_server_script],
        sandbox=False,
        timeout_seconds=15,
    )
    session = MCPClientSession(config)
    await session.connect()

    assert session.connected
    assert len(session.tools) == 1
    assert session.tools[0].name == "echo"

    result = await session.call_tool("echo", {"message": "hello"})
    assert "echo: hello" in result.content[0].text

    await session.disconnect()
    assert not session.connected


async def test_tool_call_timeout(slow_server_script: str) -> None:
    """Tool that sleeps longer than timeout raises TimeoutError."""
    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    config = MCPServerConfig(
        name="slow_test",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[slow_server_script],
        sandbox=False,
        timeout_seconds=2,
    )
    session = MCPClientSession(config)
    await session.connect()

    with pytest.raises(TimeoutError, match="timed out"):
        await session.call_tool("slow_op", {})

    await session.disconnect()


async def test_health_check_on_live_session(echo_server_script: str) -> None:
    """health_check() returns True on a connected session."""
    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    config = MCPServerConfig(
        name="echo_health",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[echo_server_script],
        sandbox=False,
        timeout_seconds=15,
    )
    session = MCPClientSession(config)
    await session.connect()

    assert await session.health_check() is True

    await session.disconnect()


# ── Sandboxed path ────────────────────────────────────────────────────────────


async def test_sandboxed_connect_and_call(echo_server_script: str) -> None:
    """Same flow through MCPSandbox: connect → call → disconnect."""
    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    config = MCPServerConfig(
        name="echo_sandboxed",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[echo_server_script],
        sandbox=True,
        sandbox_memory_mb=256,
        timeout_seconds=15,
    )
    session = MCPClientSession(config)
    await session.connect()

    assert session.connected
    assert len(session.tools) == 1

    result = await session.call_tool("echo", {"message": "sandboxed"})
    assert "echo: sandboxed" in result.content[0].text

    await session.disconnect()
    assert not session.connected


async def test_sandboxed_env_isolation(tmp_path, monkeypatch) -> None:
    """Sandboxed subprocess must not see PINCER_* env vars from parent."""

    monkeypatch.setenv("PINCER_SECRET_KEY", "must-not-leak")

    script = tmp_path / "env_server.py"
    script.write_text(
        """\
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("env-test")


@mcp.tool()
def get_env(var: str) -> str:
    \"\"\"Return an environment variable value, or MISSING if not set.\"\"\"
    return os.environ.get(var, "MISSING")


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    )

    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    config = MCPServerConfig(
        name="env_test",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[str(script)],
        sandbox=True,
        timeout_seconds=15,
    )
    session = MCPClientSession(config)
    await session.connect()

    result = await session.call_tool("get_env", {"var": "PINCER_SECRET_KEY"})
    assert result.content[0].text == "MISSING"

    await session.disconnect()
