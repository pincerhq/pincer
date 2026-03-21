"""Tests for PincerMCPServer — lifecycle, properties, and integration with exporter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.config import MCPServerExportConfig
from pincer.mcp.server import PincerMCPServer

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config(**kwargs) -> MCPServerExportConfig:
    defaults = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 18800,
        "path": "/mcp",
        "expose_tools": ["web_search"],
    }
    defaults.update(kwargs)
    return MCPServerExportConfig(**defaults)


def _make_registry(tool_names: list[str] | None = None):
    from pincer.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for name in (tool_names or ["web_search"]):
        async def _handler(**_):
            return "ok"
        _handler.__name__ = name
        reg.register(name=name, description=f"Tool {name}", handler=_handler)
    return reg


def _make_server(**kwargs) -> PincerMCPServer:
    return PincerMCPServer(
        config=_make_config(),
        tool_registry=_make_registry(),
        **kwargs,
    )


# ── Config and properties ─────────────────────────────────────────────────────


def test_server_disabled_by_default():
    cfg = MCPServerExportConfig()
    assert cfg.enabled is False


def test_server_binds_localhost_by_default():
    cfg = MCPServerExportConfig()
    assert cfg.host == "127.0.0.1"


def test_server_endpoint_property():
    server = _make_server()
    assert server.endpoint == "http://127.0.0.1:18800/mcp"


def test_server_endpoint_custom_port():
    cfg = _make_config(port=9999, path="/api/mcp")
    server = PincerMCPServer(config=cfg, tool_registry=_make_registry())
    assert server.endpoint == "http://127.0.0.1:9999/api/mcp"


def test_server_not_running_before_start():
    server = _make_server()
    assert server.running is False


def test_server_exposed_tool_count_zero_before_start():
    server = _make_server()
    assert server.exposed_tool_count == 0


# ── Startup ───────────────────────────────────────────────────────────────────


def _fmcp_patch():
    """Context manager that patches FastMCP at its source (lazy import)."""
    mock_fmcp = MagicMock()
    mock_fmcp.run_streamable_http_async = AsyncMock()
    mock_cls = MagicMock(return_value=mock_fmcp)
    return patch("mcp.server.fastmcp.FastMCP", mock_cls), mock_fmcp


async def test_server_starts_on_configured_port():
    server = _make_server()
    ctx, _fmcp = _fmcp_patch()
    with ctx, patch("pincer.mcp.exporter.PincerToolExporter.export_to", return_value=1):
        await server.start()
        assert server.running is True
        await server.stop()


async def test_server_running_property_true_after_start():
    server = _make_server()
    ctx, _fmcp = _fmcp_patch()
    with ctx, patch("pincer.mcp.exporter.PincerToolExporter.export_to", return_value=2):
        await server.start()
        assert server.running is True
        assert server.exposed_tool_count == 2
        await server.stop()


async def test_server_exposes_only_whitelisted_tools():
    """Only tools in expose_tools are passed to the exporter."""
    from pincer.mcp.exporter import PincerToolExporter

    reg = _make_registry(["web_search", "email_send"])
    cfg = _make_config(expose_tools=["web_search"])
    server = PincerMCPServer(config=cfg, tool_registry=reg)

    captured_expose: list[str] = []
    orig_init = PincerToolExporter.__init__

    def _capture_init(self, tool_registry, expose_tools, **kwargs):
        captured_expose.extend(expose_tools)
        orig_init(self, tool_registry, expose_tools, **kwargs)

    ctx, _fmcp = _fmcp_patch()
    with ctx, patch.object(PincerToolExporter, "__init__", _capture_init), \
            patch.object(PincerToolExporter, "export_to", return_value=1):
        await server.start()
        await server.stop()

    assert "web_search" in captured_expose
    assert "email_send" not in captured_expose


# ── Shutdown ──────────────────────────────────────────────────────────────────


async def test_server_stop_cleans_up():
    server = _make_server()
    ctx, _fmcp = _fmcp_patch()
    with ctx, patch("pincer.mcp.exporter.PincerToolExporter.export_to", return_value=0):
        await server.start()
        assert server.running is True
        await server.stop()
        assert server.running is False
        assert server._serve_task is None


async def test_server_stop_no_op_when_not_started():
    server = _make_server()
    # Should not raise
    await server.stop()
    assert server.running is False


async def test_stop_cancels_background_task():
    server = _make_server()
    ctx, _fmcp = _fmcp_patch()
    with ctx, patch("pincer.mcp.exporter.PincerToolExporter.export_to", return_value=0):
        await server.start()
        task = server._serve_task
        assert task is not None
        await server.stop()
        assert task.done()


# ── Audit logging ─────────────────────────────────────────────────────────────


async def test_server_logs_start_stop_audit():
    mock_audit = MagicMock()
    mock_audit.log_connect = AsyncMock()
    mock_audit.log_disconnect = AsyncMock()
    server = _make_server(audit_logger=mock_audit)

    ctx, _fmcp = _fmcp_patch()
    with ctx, patch("pincer.mcp.exporter.PincerToolExporter.export_to", return_value=0):
        await server.start()
        await server.stop()

    mock_audit.log_connect.assert_called_once_with("mcp_server_export", success=True)
    mock_audit.log_disconnect.assert_called_once_with("mcp_server_export")


# ── Import guard ──────────────────────────────────────────────────────────────


async def test_server_handles_import_error():
    """start() raises RuntimeError when mcp package is not installed."""
    server = _make_server()
    with patch.dict("sys.modules", {"mcp": None, "mcp.server": None, "mcp.server.fastmcp": None}), \
            pytest.raises(RuntimeError, match="mcp package required"):
        await server.start()
