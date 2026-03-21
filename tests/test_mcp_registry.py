"""Tests for MCPRegistryClient — search, install, scan, config generation, uninstall."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.config import MCPTransport
from pincer.mcp.registry_client import (
    MCPRegistryClient,
    MCPRegistryEntry,
    _infer_entry,
    _to_config_name,
)
from pincer.skills.scanner import PackageScanner, ScanResult

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_entry(**kwargs) -> MCPRegistryEntry:
    defaults = dict(
        package_name="@modelcontextprotocol/server-github",
        name="github",
        description="GitHub MCP server",
        registry="mcp",
        transport=MCPTransport.STDIO,
        package_manager="npm",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        required_env=["GITHUB_TOKEN"],
        version="1.0.0",
        homepage="https://github.com/modelcontextprotocol/servers",
        downloads=5000,
    )
    defaults.update(kwargs)
    return MCPRegistryEntry(**defaults)


def _mock_http_response(data: list | dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    return resp


# ── Name normalization ────────────────────────────────────────────────────────


def test_to_config_name_scoped_npm():
    assert _to_config_name("@modelcontextprotocol/server-github") == "github"


def test_to_config_name_plain():
    assert _to_config_name("server-postgres") == "postgres"


def test_to_config_name_python_package():
    assert _to_config_name("mcp-server-filesystem") == "filesystem"


def test_to_config_name_special_chars():
    name = _to_config_name("my.weird-pkg_name")
    assert name.replace("_", "").isalnum()


# ── Entry inference ───────────────────────────────────────────────────────────


def test_infer_entry_npm_package():
    entry = _infer_entry("@org/my-server")
    assert entry.package_manager == "npm"
    assert entry.command == "npx"
    assert "-y" in entry.args


def test_infer_entry_python_package():
    entry = _infer_entry("mcp_filesystem")
    assert entry.package_manager == "pip"
    assert entry.command == "python"
    assert "-m" in entry.args


# ── Config generation ─────────────────────────────────────────────────────────


def test_generate_config_npm_entry():
    client = MCPRegistryClient()
    entry = _make_entry()
    config = client._generate_config(entry)
    assert config.name == "github"
    assert config.command == "npx"
    assert config.transport == MCPTransport.STDIO
    assert config.sandbox is True
    assert config.approval_required == ["*"]


def test_generate_config_with_required_env():
    client = MCPRegistryClient()
    entry = _make_entry(required_env=["GITHUB_TOKEN", "GITHUB_ORG"])
    config = client._generate_config(entry)
    # Env vars should have placeholder values
    assert "GITHUB_TOKEN" in config.env
    assert "${GITHUB_TOKEN}" in config.env["GITHUB_TOKEN"]


def test_generate_config_python_package():
    client = MCPRegistryClient()
    entry = _make_entry(
        package_name="mcp-filesystem",
        name="filesystem",
        package_manager="pip",
        command="python",
        args=["-m", "mcp_filesystem"],
    )
    config = client._generate_config(entry)
    assert config.command == "python"
    assert "-m" in config.args


# ── Registry search (mocked HTTP) ─────────────────────────────────────────────


async def test_search_returns_normalized_results():
    client = MCPRegistryClient()
    mock_data = [
        {
            "qualifiedName": "@modelcontextprotocol/server-github",
            "name": "server-github",
            "description": "GitHub integration",
            "runtime": "node",
            "downloads": 1000,
        }
    ]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mock_http_response(mock_data))

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        results = await client.search("github", registry="mcp")

    assert len(results) >= 1
    assert results[0].registry == "mcp"
    assert results[0].command == "npx"


async def test_search_returns_empty_on_api_error():
    client = MCPRegistryClient()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("network error"))

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        results = await client.search("anything", registry="mcp")

    assert results == []


# ── AST Scanner ───────────────────────────────────────────────────────────────


def test_scanner_clean_code():
    scanner = PackageScanner()
    result = scanner.scan_source('def greet(name: str) -> str:\n    return f"Hello, {name}"')
    assert result.score == 100
    assert result.findings == []
    assert result.safe


def test_scanner_eval_critical():
    scanner = PackageScanner()
    result = scanner.scan_source("x = eval(input('expr: '))")
    assert result.score <= 60
    critical = [f for f in result.findings if f.risk == "critical"]
    assert len(critical) >= 1


def test_scanner_subprocess_high():
    scanner = PackageScanner()
    result = scanner.scan_source("import subprocess\nsubprocess.run(['ls'])")
    high = [f for f in result.findings if f.risk == "high"]
    assert len(high) >= 1


def test_scanner_env_access_medium():
    scanner = PackageScanner()
    result = scanner.scan_source("import os\ntoken = os.environ['TOKEN']")
    medium = [f for f in result.findings if f.risk == "medium"]
    assert len(medium) >= 1


def test_low_score_package_blocked():
    scanner = PackageScanner()
    dangerous = "\n".join([
        "import subprocess",
        "import socket",
        "eval(input())",
        "exec(compile('x=1','f','exec'))",
    ])
    result = scanner.scan_source(dangerous)
    assert result.blocked


# ── Install (mocked) ──────────────────────────────────────────────────────────


async def test_install_generates_config():
    client = MCPRegistryClient()
    entry = _make_entry()

    with (
        patch.object(client, "fetch_metadata", AsyncMock(return_value=entry)),
        patch.object(client, "_scan_package", AsyncMock(return_value=ScanResult(score=95))),
    ):
        config, scan_info = await client.install("@modelcontextprotocol/server-github")

    assert config.name == "github"
    assert scan_info["score"] == 95
    assert not scan_info["skipped"]


async def test_install_blocked_by_low_scan_score():
    client = MCPRegistryClient()
    entry = _make_entry()

    with (
        patch.object(client, "fetch_metadata", AsyncMock(return_value=entry)),
        patch.object(client, "_scan_package", AsyncMock(return_value=ScanResult(score=25))),
        pytest.raises(ValueError, match="blocked"),
    ):
        await client.install("@dangerous/package")


async def test_install_scan_skipped_with_no_scan():
    client = MCPRegistryClient()
    entry = _make_entry()

    with (
        patch.object(client, "fetch_metadata", AsyncMock(return_value=entry)),
        patch.object(client, "_scan_package") as mock_scan,
    ):
        config, scan_info = await client.install("@pkg/server", scan=False)

    mock_scan.assert_not_called()
    assert scan_info["skipped"] is True


# ── Uninstall ─────────────────────────────────────────────────────────────────


def test_uninstall_removes_config_entry(tmp_path: Path):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        '[mcp]\nenabled = true\n\n[[mcp.servers]]\nname = "github"\ntransport = "stdio"\ncommand = "npx"\n'
    )
    client = MCPRegistryClient()
    removed = client.uninstall("github", config_path=tmp_path)
    assert removed is True
    content = toml.read_text()
    assert 'name = "github"' not in content


def test_uninstall_returns_false_if_not_found(tmp_path: Path):
    toml = tmp_path / "pincer.toml"
    toml.write_text('[mcp]\nenabled = true\n')
    client = MCPRegistryClient()
    assert client.uninstall("nonexistent", config_path=tmp_path) is False
