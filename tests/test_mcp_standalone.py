"""Tests for StandaloneMCPShell."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.approval import (
    CLIApprovalBackend,
    PolicyApprovalBackend,
    WebhookApprovalBackend,
)
from pincer.mcp.standalone import StandaloneMCPShell


def _make_config(server_enabled=True, expose_tools=None, servers=None, webhook_url=None):
    cfg = MagicMock()
    cfg.enabled = True
    cfg.servers = servers or []

    server_cfg = MagicMock()
    server_cfg.enabled = server_enabled
    server_cfg.host = "127.0.0.1"
    server_cfg.port = 18800
    server_cfg.path = "/mcp"
    server_cfg.expose_tools = expose_tools or ["web_search"]
    server_cfg.sampling = MagicMock(enabled=False)
    server_cfg.approval_policy = MagicMock(
        as_dict=MagicMock(return_value={"default": "deny"})
    )
    server_cfg.webhook_url = webhook_url
    cfg.server = server_cfg

    return cfg


# ── Approval backend selection ─────────────────────────────────────────────────


def test_build_policy_approval_backend():
    cfg = _make_config()
    shell = StandaloneMCPShell(
        mcp_config=cfg,
        approval_mode="policy",
        approval_policy={"read": "approve", "default": "deny"},
    )
    backend = shell._build_approval_backend()
    assert isinstance(backend, PolicyApprovalBackend)


def test_build_cli_approval_backend():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg, approval_mode="cli")
    backend = shell._build_approval_backend()
    assert isinstance(backend, CLIApprovalBackend)


def test_build_webhook_approval_backend():
    cfg = _make_config()
    shell = StandaloneMCPShell(
        mcp_config=cfg,
        approval_mode="webhook",
        webhook_url="http://example.com/approve",
    )
    backend = shell._build_approval_backend()
    assert isinstance(backend, WebhookApprovalBackend)


def test_build_webhook_without_url_raises():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg, approval_mode="webhook", webhook_url=None)
    with pytest.raises(ValueError, match="webhook_url"):
        shell._build_approval_backend()


def test_default_approval_mode_is_policy():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg)
    backend = shell._build_approval_backend()
    assert isinstance(backend, PolicyApprovalBackend)


# ── LLM backend ────────────────────────────────────────────────────────────────


def test_build_llm_backend_sampling_disabled():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg)
    llm = shell._build_llm_backend()
    assert llm is None


def test_build_llm_backend_sampling_enabled():
    cfg = _make_config()
    cfg.server.sampling.enabled = True
    cfg.server.sampling.model = "claude-haiku-4-5-20251001"

    shell = StandaloneMCPShell(mcp_config=cfg, llm_api_key="sk-test")

    with patch("pincer.mcp.standalone.StandaloneLLMBackend") as mock_backend:  # noqa: N806
        mock_backend.return_value = MagicMock()
        llm = shell._build_llm_backend()
        mock_backend.assert_called_once()
        assert llm is not None


# ── run() lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_standalone_run_server_not_enabled():
    cfg = _make_config(server_enabled=False)
    shell = StandaloneMCPShell(mcp_config=cfg)
    # Should return immediately (server not enabled)
    await shell.run()
    assert not shell.server_running


@pytest.mark.asyncio
async def test_standalone_run_starts_and_stops_server():
    cfg = _make_config(server_enabled=True)
    shell = StandaloneMCPShell(mcp_config=cfg)

    mock_server = AsyncMock()
    mock_server.running = True
    mock_server.endpoint = "http://127.0.0.1:18800/mcp"

    # Cancel run() after it starts by raising CancelledError from Event.wait()
    event_mock = MagicMock()
    event_mock.wait = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("pincer.mcp.core.PincerMCPServer", return_value=mock_server), \
         patch("asyncio.Event", return_value=event_mock), \
         patch.object(shell, "_register_standalone_tools"):
        await shell.run()

    mock_server.start.assert_called_once()
    mock_server.stop.assert_called_once()


@pytest.mark.asyncio
async def test_standalone_registers_builtin_prompts():
    cfg = _make_config(server_enabled=True)
    shell = StandaloneMCPShell(mcp_config=cfg)

    mock_server = AsyncMock()
    mock_server.running = True
    mock_server.endpoint = "http://127.0.0.1:18800/mcp"

    event_mock = MagicMock()
    event_mock.wait = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("pincer.mcp.core.PincerMCPServer", return_value=mock_server), \
         patch("asyncio.Event", return_value=event_mock), \
         patch.object(shell, "_register_standalone_tools"):
        await shell.run()

    core = shell._core
    if core is None:
        return  # cleaned up in finally block

    # Prompts should include built-ins (registered before run was stopped)
    prompt_names = [p.name for p in core.prompt_registry.list_all()]
    assert "pincer_summarize" in prompt_names


# ── Properties ────────────────────────────────────────────────────────────────


def test_server_running_false_before_start():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg)
    assert not shell.server_running


def test_server_endpoint_none_before_start():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg)
    assert shell.server_endpoint is None


def test_core_none_before_start():
    cfg = _make_config()
    shell = StandaloneMCPShell(mcp_config=cfg)
    assert shell.core is None
