"""Tests for the doctr-mcp server module: health endpoint, CLI parsing, meta tool."""

from __future__ import annotations

import pytest


def test_health_endpoint() -> None:
    from doctr_mcp.server import mcp
    from starlette.testclient import TestClient

    app = mcp.http_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "doctr-mcp"


def test_root_endpoint() -> None:
    from doctr_mcp.server import mcp
    from starlette.testclient import TestClient

    app = mcp.http_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_instructions_warn_against_self_reading_pdfs() -> None:
    """The server's instructions must steer callers away from reading PDF/image
    files with generic tools instead of this server's OCR tools — this text is
    surfaced into Pincer's own system prompt via MCPClientSession.instructions."""
    from doctr_mcp.server import mcp

    assert mcp.instructions is not None
    assert "file_read" in mcp.instructions
    assert "shell_exec" in mcp.instructions
    assert "python_exec" in mcp.instructions


def test_instructions_warn_against_self_vision_transcription() -> None:
    """A vision-capable model that already 'sees' the image has no reason not
    to just transcribe it itself unless explicitly told otherwise — generic
    file-tool warnings alone (file_read/shell_exec/python_exec) don't cover
    that case."""
    from doctr_mcp.server import mcp

    assert mcp.instructions is not None
    assert "vision" in mcp.instructions.lower()


def test_critical_instruction_survives_default_truncation() -> None:
    """Pincer truncates each connected server's instructions to a per-server
    character budget (mcp_instructions_max_chars, 400 by default) before
    surfacing them into its own system prompt. The directive that actually
    changes model behavior must land within that budget, or it never reaches
    the model at all."""
    from doctr_mcp.server import mcp

    assert mcp.instructions is not None
    collapsed = " ".join(mcp.instructions.split())
    truncated = collapsed[:400]
    assert "do not transcribe" in truncated.lower()
    assert "file_read" in truncated


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp import server

    monkeypatch.delenv("TRANSPORT", raising=False)
    monkeypatch.setattr("sys.argv", ["doctr-mcp-run"])
    args = server._parse_args()  # noqa: SLF001
    assert args.transport == "http"  # this server defaults to HTTP, unlike the other bundled servers
    assert args.host == "0.0.0.0"
    assert args.port == 8000


def test_parse_args_stdio_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp import server

    monkeypatch.setattr("sys.argv", ["doctr-mcp-run", "--transport", "stdio", "--port", "9001"])
    args = server._parse_args()  # noqa: SLF001
    assert args.transport == "stdio"
    assert args.port == 9001


def test_parse_args_reads_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp import server

    monkeypatch.setenv("TRANSPORT", "stdio")
    monkeypatch.setattr("sys.argv", ["doctr-mcp-run"])
    args = server._parse_args()  # noqa: SLF001
    assert args.transport == "stdio"


@pytest.mark.asyncio
async def test_list_architectures_tool() -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool("list_architectures", {"params": {}})
    data = result.structured_content
    assert data is not None
    assert "db_resnet50" in data["detection"]
    assert "crnn_vgg16_bn" in data["recognition"]
    assert data["defaults"]["det_arch"] == "db_resnet50"


def test_log_tool_requests_disabled_by_default() -> None:
    from doctr_mcp.server import mcp
    from fastmcp.server.middleware.logging import LoggingMiddleware

    assert not any(isinstance(m, LoggingMiddleware) for m in mcp.middleware)


def test_log_tool_requests_attaches_logging_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from doctr_mcp import server
    from doctr_mcp.config import get_settings
    from fastmcp.server.middleware.logging import LoggingMiddleware

    monkeypatch.setenv("DOCTR_LOG_TOOL_REQUESTS", "true")
    get_settings.cache_clear()
    try:
        importlib.reload(server)
        assert any(isinstance(m, LoggingMiddleware) for m in server.mcp.middleware)
    finally:
        monkeypatch.delenv("DOCTR_LOG_TOOL_REQUESTS", raising=False)
        get_settings.cache_clear()
        importlib.reload(server)


@pytest.mark.asyncio
async def test_log_tool_requests_logs_call_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib
    import logging as logging_module

    from doctr_mcp import server
    from doctr_mcp.config import get_settings

    monkeypatch.setenv("DOCTR_LOG_TOOL_REQUESTS", "true")
    get_settings.cache_clear()
    try:
        importlib.reload(server)
        with caplog.at_level(logging_module.INFO, logger="doctr_mcp.requests"):
            await server.mcp.call_tool("list_architectures", {"params": {}})
        records = [r for r in caplog.records if r.name == "doctr_mcp.requests"]
        assert records
        assert any("list_architectures" in r.message for r in records)
    finally:
        monkeypatch.delenv("DOCTR_LOG_TOOL_REQUESTS", raising=False)
        get_settings.cache_clear()
        importlib.reload(server)


@pytest.mark.asyncio
async def test_log_tool_requests_truncates_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib
    import logging as logging_module

    from doctr_mcp import server
    from doctr_mcp.config import get_settings

    monkeypatch.setenv("DOCTR_LOG_TOOL_REQUESTS", "true")
    monkeypatch.setenv("DOCTR_LOG_MAX_PAYLOAD_LENGTH", "20")
    get_settings.cache_clear()
    try:
        importlib.reload(server)
        with caplog.at_level(logging_module.INFO, logger="doctr_mcp.requests"):
            await server.mcp.call_tool("list_architectures", {"params": {}})
        request_records = [r for r in caplog.records if r.name == "doctr_mcp.requests" and "request_start" in r.message]
        assert request_records
        # The truncated payload (20 chars + "...") must be far shorter than the
        # untruncated CallToolRequestParams JSON would be.
        assert all("payload=" in r.message and len(r.message) < 200 for r in request_records)
    finally:
        monkeypatch.delenv("DOCTR_LOG_TOOL_REQUESTS", raising=False)
        monkeypatch.delenv("DOCTR_LOG_MAX_PAYLOAD_LENGTH", raising=False)
        get_settings.cache_clear()
        importlib.reload(server)
