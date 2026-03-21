"""Tests for MCPClientSession — connection, tool discovery, timeout, health check."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.client import MCPClientSession
from pincer.mcp.config import MCPServerConfig, MCPTransport


def _make_stdio_config(sandbox: bool = True) -> MCPServerConfig:
    return MCPServerConfig(
        name="testserver",
        transport=MCPTransport.STDIO,
        command="echo",
        args=["hello"],
        sandbox=sandbox,
        timeout_seconds=5,
        max_retries=1,
    )


def _make_http_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="httpserver",
        transport=MCPTransport.STREAMABLE_HTTP,
        url="https://example.com/mcp",
        timeout_seconds=5,
        max_retries=1,
    )


# ── Basic properties ──────────────────────────────────────────────────────────


def test_initial_state() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    assert session.name == "testserver"
    assert session.connected is False
    assert session.tools == []


def test_name_matches_config() -> None:
    cfg = _make_http_config()
    session = MCPClientSession(cfg)
    assert session.name == "httpserver"


# ── Connection error handling ─────────────────────────────────────────────────


async def test_connect_failure_raises_connection_error() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    with patch("pincer.mcp.client.MCPClientSession._connect_stdio", side_effect=OSError("not found")):
        with pytest.raises(ConnectionError, match="connection failed"):
            await session.connect()

    assert session.connected is False
    assert session._connect_attempts == 1


async def test_connect_increments_attempts_on_failure() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    with patch("pincer.mcp.client.MCPClientSession._connect_stdio", side_effect=OSError("fail")):
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await session.connect()

    assert session._connect_attempts == 3


# ── tool call ────────────────────────────────────────────────────────────────


async def test_call_tool_raises_when_not_connected() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    with pytest.raises(ConnectionError, match="not connected"):
        await session.call_tool("my_tool", {})


async def test_call_tool_raises_timeout() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    session._session = MagicMock()

    async def _slow_call(*args, **kwargs):
        await asyncio.sleep(10)

    session._session.call_tool = _slow_call
    session.config = MCPServerConfig(
        name="testserver",
        transport=MCPTransport.STDIO,
        command="echo",
        timeout_seconds=1,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await session.call_tool("slow_tool", {})


# ── Health check ──────────────────────────────────────────────────────────────


async def test_health_check_returns_false_when_disconnected() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    assert await session.health_check() is False


async def test_health_check_returns_true_on_successful_ping() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    mock_mcp_session = MagicMock()
    mock_mcp_session.send_ping = AsyncMock(return_value=None)
    session._session = mock_mcp_session

    result = await session.health_check()
    assert result is True


async def test_health_check_returns_false_on_ping_failure() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    mock_mcp_session = MagicMock()
    mock_mcp_session.send_ping = AsyncMock(side_effect=Exception("connection lost"))
    session._session = mock_mcp_session

    result = await session.health_check()
    assert result is False
    assert session._connected is False


# ── Sandbox env ───────────────────────────────────────────────────────────────


def test_sandbox_env_excludes_pincer_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("MY_TOKEN", "ghp_123")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        env={"MY_TOKEN": "ghp_123"},
        sandbox=True,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    assert env is not None
    assert "PINCER_ANTHROPIC_API_KEY" not in env
    assert env.get("MY_TOKEN") == "ghp_123"


def test_unsandboxed_no_extra_env_returns_none_inheriting_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox=False + no declared env → return None (subprocess inherits full env)."""
    monkeypatch.setenv("SOME_VAR", "some_value")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    # None means the subprocess inherits the full process environment (including SOME_VAR)
    assert env is None


def test_unsandboxed_with_declared_env_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox=False + declared env → merge declared vars on top of current env."""
    monkeypatch.setenv("EXISTING_VAR", "existing")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        env={"MY_TOKEN": "tok123"},
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    assert env is not None
    assert env.get("MY_TOKEN") == "tok123"
    assert env.get("EXISTING_VAR") == "existing"


def test_unsandboxed_no_extra_env_returns_none() -> None:
    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()
    # No extra env declared, sandbox=False → returns None (inherit all)
    assert env is None
