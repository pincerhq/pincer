"""Tests for MCPClientManager — lifecycle, parallel startup, health loop, reconnection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.config import MCPConfig, MCPServerConfig, MCPTransport
from pincer.mcp.manager import MCPClientManager
from pincer.tools.registry import ToolRegistry


def _make_config(*names: str) -> MCPConfig:
    servers = [MCPServerConfig(name=n, transport=MCPTransport.STDIO, command="echo") for n in names]
    return MCPConfig(servers=servers)


def _make_manager(cfg: MCPConfig) -> MCPClientManager:
    registry = ToolRegistry()
    return MCPClientManager(cfg, registry, audit_logger=None)


# ── Disabled config ───────────────────────────────────────────────────────────


async def test_start_returns_empty_when_disabled() -> None:
    cfg = MCPConfig(enabled=False)
    manager = _make_manager(cfg)
    results = await manager.start()
    assert results == {}


async def test_start_returns_empty_when_no_servers() -> None:
    cfg = MCPConfig(enabled=True, servers=[])
    manager = _make_manager(cfg)
    results = await manager.start()
    assert results == {}


# ── Successful startup ────────────────────────────────────────────────────────


async def test_start_connects_all_servers() -> None:
    cfg = _make_config("server1", "server2")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session.health_check = AsyncMock(return_value=True)

    with patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session):
        results = await manager.start()

    assert results == {"server1": True, "server2": True}
    await manager.stop()


# ── Failed startup (graceful degradation) ────────────────────────────────────


async def test_failed_server_does_not_block_startup() -> None:
    cfg = _make_config("good", "bad")
    manager = _make_manager(cfg)

    call_count = 0

    async def connect_or_fail():
        nonlocal call_count
        call_count += 1
        if call_count % 2 == 0:  # "bad" server
            raise ConnectionError("refused")

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = connect_or_fail
    mock_session.disconnect = AsyncMock()

    with patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session):
        results = await manager.start()

    # One succeeded, one failed
    assert True in results.values()
    assert False in results.values()
    await manager.stop()


# ── Stop / cleanup ────────────────────────────────────────────────────────────


async def test_stop_deregisters_tools() -> None:
    async def _noop_handler() -> str:
        return ""

    registry = ToolRegistry()
    registry.register(
        name="github__list_repos",
        description="List repos",
        handler=_noop_handler,
    )
    cfg = _make_config("github")
    manager = MCPClientManager(cfg, registry, audit_logger=None)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()

    mock_bridge = MagicMock()
    mock_bridge.register_tools = MagicMock(return_value=0)
    mock_bridge.deregister_tools = MagicMock()

    with (
        patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session),
        patch("pincer.mcp.manager.MCPToolBridge", return_value=mock_bridge),
    ):
        await manager.start()
        await manager.stop()

    mock_bridge.deregister_tools.assert_called()


# ── Session access ────────────────────────────────────────────────────────────


async def test_get_session_raises_for_unknown_server() -> None:
    cfg = MCPConfig()
    manager = _make_manager(cfg)
    with pytest.raises(ValueError, match="not connected"):
        await manager.get_session("nonexistent")


async def test_get_session_returns_connected_session() -> None:
    cfg = _make_config("myserver")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()

    with (
        patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session),
        patch("pincer.mcp.manager.MCPToolBridge"),
    ):
        await manager.start()
        session = await manager.get_session("myserver")

    assert session is mock_session
    await manager.stop()


# ── list_servers ──────────────────────────────────────────────────────────────


async def test_list_servers_includes_all_configured() -> None:
    cfg = _make_config("srv1", "srv2")
    manager = _make_manager(cfg)

    statuses = await manager.list_servers()
    names = [s["name"] for s in statuses]
    assert "srv1" in names
    assert "srv2" in names
    # Not connected yet → all show False
    for s in statuses:
        assert s["connected"] is False


# ── Reconnection ──────────────────────────────────────────────────────────────


async def test_disabled_after_max_retries_exceeded() -> None:
    cfg = _make_config("fragile")
    manager = _make_manager(cfg)

    failing_session = MagicMock()
    failing_session.config = cfg.servers[0]
    failing_session.connect = AsyncMock(side_effect=ConnectionError("always fails"))
    failing_session.disconnect = AsyncMock()

    manager._sessions["fragile"] = failing_session

    # Simulate reconnect — will exhaust max_retries=2
    await manager._reconnect("fragile")

    assert "fragile" in manager._disabled


async def test_start_skips_disabled_servers() -> None:
    """Servers with enabled=False are not connected."""
    from pincer.mcp.config import MCPServerConfig, MCPTransport

    servers = [
        MCPServerConfig(name="active", transport=MCPTransport.STDIO, command="echo"),
        MCPServerConfig(name="inactive", transport=MCPTransport.STDIO, command="echo", enabled=False),
    ]
    from pincer.mcp.config import MCPConfig

    cfg = MCPConfig(servers=servers)
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()

    with patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session):
        results = await manager.start()

    assert "active" in results
    assert "inactive" not in results
    await manager.stop()


async def test_stop_cancels_health_task() -> None:
    """stop() cancels the background health loop task."""
    cfg = _make_config("srv")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = []
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()

    with (
        patch("pincer.mcp.manager.MCPClientSession", return_value=mock_session),
        patch("pincer.mcp.manager.MCPToolBridge"),
    ):
        await manager.start()
        assert manager._task_group is not None
        assert manager._task_group_ctx is not None
        await manager.stop()
        assert manager._task_group is None


async def test_health_loop_reconnects_on_failure() -> None:
    """When health check fails, _reconnect is called for that server."""
    cfg = _make_config("srv")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.health_check = AsyncMock(return_value=False)

    manager._sessions["srv"] = mock_session

    reconnect_called = []

    async def fake_reconnect(name: str) -> None:
        reconnect_called.append(name)

    manager._reconnect = fake_reconnect  # type: ignore[method-assign]
    await manager._run_health_checks()

    assert "srv" in reconnect_called


async def test_tools_changed_re_registers() -> None:
    """_handle_tools_changed refreshes tools on the session and updates bridge."""
    cfg = _make_config("srv")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.refresh_tools = AsyncMock()

    mock_bridge = MagicMock()
    mock_bridge.update_tools = MagicMock(return_value=3)

    manager._sessions["srv"] = mock_session
    manager._bridges["srv"] = mock_bridge

    await manager._handle_tools_changed("srv")

    mock_session.refresh_tools.assert_called_once()
    mock_bridge.update_tools.assert_called_once()


async def test_tools_changed_unknown_server_no_error() -> None:
    """_handle_tools_changed for an unknown server logs a warning but doesn't raise."""
    cfg = MCPConfig()
    manager = _make_manager(cfg)
    # Should not raise
    await manager._handle_tools_changed("unknown")


async def test_total_tool_count() -> None:
    """total_tool_count() returns sum of tools across connected sessions."""
    cfg = _make_config("srv")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.tools = [MagicMock(), MagicMock(), MagicMock()]  # 3 tools

    manager._sessions["srv"] = mock_session
    assert manager.total_tool_count() == 3


async def test_stop_disconnect_exception_is_handled() -> None:
    """If a session raises during disconnect in stop(), it's caught and logged."""
    cfg = _make_config("flaky")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.disconnect = AsyncMock(side_effect=RuntimeError("gone"))

    mock_bridge = MagicMock()
    mock_bridge.deregister_tools = MagicMock()

    manager._sessions["flaky"] = mock_session
    manager._bridges["flaky"] = mock_bridge

    # Should not raise
    await manager.stop()
    mock_bridge.deregister_tools.assert_called()


async def test_run_health_checks_skips_disabled() -> None:
    """_run_health_checks does not ping servers in the disabled set."""
    cfg = _make_config("broken")
    manager = _make_manager(cfg)

    mock_session = MagicMock()
    mock_session.health_check = AsyncMock(return_value=False)
    manager._sessions["broken"] = mock_session
    manager._disabled.add("broken")

    await manager._run_health_checks()
    mock_session.health_check.assert_not_called()


async def test_reconnect_no_op_for_unknown_server() -> None:
    """_reconnect returns immediately if the server is not in _sessions."""
    cfg = MCPConfig()
    manager = _make_manager(cfg)
    # Should not raise
    await manager._reconnect("ghost")


async def test_reconnect_succeeds_on_first_attempt() -> None:
    """Successful reconnect updates the session and bridge."""
    cfg = _make_config("recoverable")
    manager = _make_manager(cfg)

    old_session = MagicMock()
    old_session.config = cfg.servers[0]
    old_session.disconnect = AsyncMock()

    new_session = MagicMock()
    new_session.connected = True
    new_session.tools = []
    new_session.connect = AsyncMock()

    old_bridge = MagicMock()
    old_bridge.deregister_tools = MagicMock()

    manager._sessions["recoverable"] = old_session
    manager._bridges["recoverable"] = old_bridge

    with (
        patch("pincer.mcp.manager.MCPClientSession", return_value=new_session),
        patch("pincer.mcp.manager.MCPToolBridge") as mock_bridge_cls,
    ):
        mock_new_bridge = MagicMock()
        mock_new_bridge.register_tools = MagicMock(return_value=0)
        mock_bridge_cls.return_value = mock_new_bridge
        await manager._reconnect("recoverable")

    assert manager._sessions["recoverable"] is new_session
    old_bridge.deregister_tools.assert_called_once()
    mock_new_bridge.register_tools.assert_called_once()


async def test_disabled_deregisters_bridge_tools() -> None:
    """When max retries are exhausted, the bridge's tools are deregistered."""
    cfg = _make_config("doomed")
    manager = _make_manager(cfg)

    failing_session = MagicMock()
    failing_session.config = cfg.servers[0]
    failing_session.connect = AsyncMock(side_effect=ConnectionError("always fails"))

    mock_bridge = MagicMock()
    mock_bridge.deregister_tools = MagicMock()

    manager._sessions["doomed"] = failing_session
    manager._bridges["doomed"] = mock_bridge

    await manager._reconnect("doomed")

    assert "doomed" in manager._disabled
    mock_bridge.deregister_tools.assert_called()


async def test_health_loop_suppresses_generic_exceptions() -> None:
    """Generic exceptions from _run_health_checks don't kill the health loop task."""
    cfg = _make_config("srv")
    manager = _make_manager(cfg)

    call_count = 0

    async def failing_health_checks():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("transient error")

    # Start the health loop as a real task so we can cancel it from outside
    with (
        patch.object(manager, "_run_health_checks", failing_health_checks),
        patch("pincer.mcp.manager._HEALTH_INTERVAL_SECONDS", 0),
    ):
        task = asyncio.create_task(manager._health_loop())
        # Give the loop a few iterations
        for _ in range(5):
            await asyncio.sleep(0)
        import contextlib

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Loop should have called health checks multiple times without dying
    assert call_count >= 2
