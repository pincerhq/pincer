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
