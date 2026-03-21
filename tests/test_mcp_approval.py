"""Tests for MCP approval backends."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ChannelApprovalBackend,
    CLIApprovalBackend,
    PolicyApprovalBackend,
    WebhookApprovalBackend,
    classify_risk,
)


def _make_request(**kwargs) -> ApprovalRequest:
    defaults = {
        "tool_name": "web_search",
        "arguments": {"query": "test"},
        "source": "mcp_client:test",
        "risk_level": "read",
        "description": "Search the web for 'test'",
    }
    defaults.update(kwargs)
    return ApprovalRequest(**defaults)


# ── classify_risk ──────────────────────────────────────────────────────────────


def test_classify_risk_read():
    assert classify_risk("web_search") == "read"
    assert classify_risk("file_read") == "read"
    assert classify_risk("list_files") == "read"


def test_classify_risk_write():
    assert classify_risk("file_write") == "write"
    assert classify_risk("email_send") == "write"


def test_classify_risk_destructive():
    assert classify_risk("shell_exec") == "destructive"
    assert classify_risk("python_exec") == "destructive"
    assert classify_risk("delete_file") == "destructive"


def test_classify_risk_unknown():
    assert classify_risk("frobnicate") == "unknown"


# ── PolicyApprovalBackend ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_read_approved():
    backend = PolicyApprovalBackend({"read": "approve", "default": "deny"})
    req = _make_request(risk_level="read")
    assert await backend.request_approval(req) == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_policy_write_denied():
    backend = PolicyApprovalBackend({"read": "approve", "write": "deny", "default": "deny"})
    req = _make_request(risk_level="write")
    assert await backend.request_approval(req) == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_policy_default_deny():
    backend = PolicyApprovalBackend({"default": "deny"})
    req = _make_request(risk_level="destructive")
    assert await backend.request_approval(req) == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_policy_default_approve():
    backend = PolicyApprovalBackend({"default": "approve"})
    req = _make_request(risk_level="unknown")
    assert await backend.request_approval(req) == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_policy_empty_policy_denies_all():
    backend = PolicyApprovalBackend({})
    req = _make_request(risk_level="read")
    assert await backend.request_approval(req) == ApprovalDecision.DENIED


# ── CLIApprovalBackend ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_approval_yes():
    backend = CLIApprovalBackend()
    req = _make_request()
    with patch("builtins.input", return_value="y"):
        result = await backend.request_approval(req)
    assert result == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_cli_approval_yes_upper():
    backend = CLIApprovalBackend()
    req = _make_request()
    with patch("builtins.input", return_value="YES"):
        result = await backend.request_approval(req)
    assert result == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_cli_approval_no():
    backend = CLIApprovalBackend()
    req = _make_request()
    with patch("builtins.input", return_value="n"):
        result = await backend.request_approval(req)
    assert result == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_cli_approval_empty_denies():
    backend = CLIApprovalBackend()
    req = _make_request()
    with patch("builtins.input", return_value=""):
        result = await backend.request_approval(req)
    assert result == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_cli_approval_eof_denies():
    backend = CLIApprovalBackend()
    req = _make_request()
    with patch("builtins.input", side_effect=EOFError):
        result = await backend.request_approval(req)
    assert result == ApprovalDecision.DENIED


# ── ChannelApprovalBackend ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_approval_approved():
    async def ask_cb(user_id, channel, question):
        return "yes"

    backend = ChannelApprovalBackend(get_ask_callback=lambda: ask_cb, timeout=5)
    req = _make_request()
    result = await backend.request_approval(req)
    assert result == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_channel_approval_denied():
    async def ask_cb(user_id, channel, question):
        return "no"

    backend = ChannelApprovalBackend(get_ask_callback=lambda: ask_cb, timeout=5)
    req = _make_request()
    result = await backend.request_approval(req)
    assert result == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_channel_approval_no_channel_denies():
    backend = ChannelApprovalBackend(get_ask_callback=lambda: None)
    req = _make_request()
    result = await backend.request_approval(req)
    assert result == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_channel_approval_timeout():
    async def slow_ask(user_id, channel, question):
        await asyncio.sleep(10)
        return "yes"

    backend = ChannelApprovalBackend(get_ask_callback=lambda: slow_ask, timeout=0)
    req = _make_request()
    result = await backend.request_approval(req)
    assert result == ApprovalDecision.TIMEOUT


@pytest.mark.asyncio
async def test_channel_approval_emoji_yes():
    async def ask_cb(user_id, channel, question):
        return "✅"

    backend = ChannelApprovalBackend(get_ask_callback=lambda: ask_cb, timeout=5)
    req = _make_request()
    result = await backend.request_approval(req)
    assert result == ApprovalDecision.APPROVED


# ── WebhookApprovalBackend ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_approval_approved():
    """Simulates a webhook that immediately returns an approval decision."""
    backend = WebhookApprovalBackend(
        webhook_url="http://localhost:9999/approve",
        poll_interval=0,
        timeout=5,
    )
    req = _make_request()

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json = MagicMock(return_value={"ticket_id": "abc123"})

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json = MagicMock(return_value={"decision": "approved"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_post_resp)
    mock_client.get = AsyncMock(return_value=mock_get_resp)

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_context.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_context):
        result = await backend.request_approval(req)

    assert result == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_webhook_approval_denied():
    backend = WebhookApprovalBackend(
        webhook_url="http://localhost:9999/approve",
        poll_interval=0,
        timeout=5,
    )
    req = _make_request()

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json = MagicMock(return_value={"ticket_id": "xyz"})

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json = MagicMock(return_value={"decision": "denied"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_post_resp)
    mock_client.get = AsyncMock(return_value=mock_get_resp)

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_context.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_context):
        result = await backend.request_approval(req)

    assert result == ApprovalDecision.DENIED


@pytest.mark.asyncio
async def test_webhook_approval_import_error_denies():
    backend = WebhookApprovalBackend(webhook_url="http://localhost:9999/approve")
    req = _make_request()

    with patch.dict("sys.modules", {"httpx": None}):
        result = await backend.request_approval(req)

    assert result == ApprovalDecision.DENIED
