"""Tests for `pincer mcp` (typer.testing.CliRunner, mocked MCP client/registry)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


def _mk_server(
    name: str,
    transport: str = "stdio",
    enabled: bool = True,
    sandbox: bool = True,
    approval_required: list[str] | None = None,
    timeout: int = 30,
    command: str = "npx",
    url: str | None = None,
) -> MagicMock:
    srv = MagicMock()
    srv.name = name
    srv.transport.value = transport
    srv.enabled = enabled
    srv.sandbox = sandbox
    srv.approval_required = approval_required or []
    srv.timeout = timeout
    srv.command = command
    srv.url = url
    return srv


# ── mcp list ──────────────────────────────────────────────────────────────


def test_mcp_list_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.enabled = False
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "list"])

    assert result.exit_code == 0
    assert "MCP disabled" in result.output


def test_mcp_list_no_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.enabled = True
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "list"])

    assert result.exit_code == 0
    assert "No MCP servers configured." in result.output


def test_mcp_list_shows_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.enabled = True
    mock_cfg.servers = [_mk_server("github")]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "list"])

    assert result.exit_code == 0
    assert "github" in result.output


# ── mcp test ──────────────────────────────────────────────────────────────


def test_mcp_test_server_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "test", "github"])

    assert result.exit_code == 1
    assert "not found in config" in result.output


def test_mcp_test_connects_and_lists_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_tool = MagicMock()
    mock_tool.name = "search_repositories"
    mock_tool.description = "Search repos"

    mock_session = MagicMock()
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session.tools = [mock_tool]
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)

    result = runner.invoke(app, ["mcp", "test", "github"])

    assert result.exit_code == 0
    assert "Connected!" in result.output
    assert "search_repositories" in result.output
    mock_session.disconnect.assert_awaited_once()


def test_mcp_test_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_session = MagicMock()
    mock_session.connect = AsyncMock(side_effect=RuntimeError("timeout"))
    mock_session.disconnect = AsyncMock()
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)

    result = runner.invoke(app, ["mcp", "test", "github"])

    assert result.exit_code == 1
    assert "Connection failed" in result.output
    mock_session.disconnect.assert_awaited_once()


# ── mcp tools ─────────────────────────────────────────────────────────────


def test_mcp_tools_filter_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "tools", "--server", "missing"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_mcp_tools_lists_with_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github", approval_required=["write"])
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_tool = MagicMock()
    mock_tool.name = "create_issue"
    mock_tool.description = "Create an issue"

    mock_session = MagicMock()
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session.tools = [mock_tool]
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)
    monkeypatch.setattr("pincer.mcp.bridge._requires_approval", lambda tool, required: True)

    result = runner.invoke(app, ["mcp", "tools"])

    assert result.exit_code == 0
    assert "create_issue" in result.output
    assert "required" in result.output


def test_mcp_tools_handles_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("flaky")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_session = MagicMock()
    mock_session.connect = AsyncMock(side_effect=RuntimeError("down"))
    mock_session.disconnect = AsyncMock()
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)

    result = runner.invoke(app, ["mcp", "tools"])

    assert result.exit_code == 0
    assert "(failed)" in result.output


# ── mcp call ──────────────────────────────────────────────────────────────


def test_mcp_call_server_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "call", "github", "search"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_mcp_call_invalid_argument_format(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "call", "github", "search", "--arg", "badarg"])

    assert result.exit_code == 1
    assert "Invalid argument format" in result.output


def test_mcp_call_success(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_session = MagicMock()
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)
    monkeypatch.setattr("pincer.mcp.bridge._format_result", lambda result: str(result))

    result = runner.invoke(app, ["mcp", "call", "github", "search", "--arg", "query=pincer", "--arg", "limit=5"])

    assert result.exit_code == 0
    assert "Result:" in result.output
    mock_session.call_tool.assert_awaited_once_with("search", {"query": "pincer", "limit": 5})


def test_mcp_call_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _mk_server("github")
    mock_cfg = MagicMock()
    mock_cfg.servers = [srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_session = MagicMock()
    mock_session.connect = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", lambda srv: mock_session)

    result = runner.invoke(app, ["mcp", "call", "github", "search"])

    assert result.exit_code == 1
    assert "Error:" in result.output


# ── mcp status ────────────────────────────────────────────────────────────


def test_mcp_status_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.enabled = False
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0
    assert "MCP disabled" in result.output


def test_mcp_status_no_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.enabled = True
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0
    assert "No MCP servers configured." in result.output


def test_mcp_status_mixed_results(monkeypatch: pytest.MonkeyPatch) -> None:
    connected_srv = _mk_server("github")
    disabled_srv = _mk_server("disabled-one", enabled=False)
    failing_srv = _mk_server("flaky")

    mock_cfg = MagicMock()
    mock_cfg.enabled = True
    mock_cfg.servers = [connected_srv, disabled_srv, failing_srv]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_tool = MagicMock()

    def _session_factory(srv: object) -> MagicMock:
        session = MagicMock()
        if srv is failing_srv:
            session.connect = AsyncMock(side_effect=RuntimeError("unreachable"))
        else:
            session.connect = AsyncMock()
        session.disconnect = AsyncMock()
        session.tools = [mock_tool]
        return session

    monkeypatch.setattr("pincer.mcp.client.MCPClientSession", _session_factory)

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0
    assert "github" in result.output
    assert "disabled" in result.output
    assert "unreachable" in result.output


# ── mcp search ────────────────────────────────────────────────────────────


def test_mcp_search_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.search = AsyncMock(return_value=[])
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "search", "github"])

    assert result.exit_code == 0
    assert "No results found." in result.output


def test_mcp_search_shows_results(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = MagicMock()
    entry.package_name = "@modelcontextprotocol/server-github"
    entry.name = "github"
    entry.registry = "mcp"
    entry.description = "GitHub MCP server"
    entry.downloads = 1000

    mock_client = MagicMock()
    mock_client.search = AsyncMock(return_value=[entry])
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "search", "github"])

    assert result.exit_code == 0
    assert "github" in result.output
    assert "Found 1 result" in result.output


def test_mcp_search_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.search = AsyncMock(side_effect=RuntimeError("network error"))
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "search", "github"])

    assert result.exit_code == 1
    assert "Search failed" in result.output


# ── mcp install ───────────────────────────────────────────────────────────


def test_mcp_install_success_high_score(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    config = MagicMock()
    config.name = "github"
    config.command = "npx"
    config.args = ["-y", "@modelcontextprotocol/server-github"]
    config.env = {}
    config.transport.value = "stdio"

    scan_info = {"skipped": False, "score": 95, "summary": "Score 95/100"}

    mock_client = MagicMock()
    mock_client.install = AsyncMock(return_value=(config, scan_info))
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "install", "github-mcp"])

    assert result.exit_code == 0
    assert "Installed 'github'" in result.output
    toml_path = tmp_path / "pincer.toml"
    assert toml_path.exists()
    assert 'name = "github"' in toml_path.read_text()


def test_mcp_install_low_score_declined(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    config = MagicMock()
    config.name = "sketchy"
    config.command = "npx"
    config.args = []
    config.env = {}
    config.transport.value = "stdio"

    scan_info = {"skipped": False, "score": 20, "summary": "Score 20/100"}

    mock_client = MagicMock()
    mock_client.install = AsyncMock(return_value=(config, scan_info))
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "install", "sketchy-mcp"], input="n\n")

    assert result.exit_code == 0
    assert "Installation cancelled." in result.output
    assert not (tmp_path / "pincer.toml").exists()


def test_mcp_install_blocked_by_scan(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    mock_client = MagicMock()
    mock_client.install = AsyncMock(side_effect=ValueError("malicious code detected"))
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "install", "bad-mcp"])

    assert result.exit_code == 1
    assert "blocked by security scan" in result.output


# ── mcp scan ──────────────────────────────────────────────────────────────


def test_mcp_scan_local_path_safe(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    scan_target = tmp_path / "myserver"
    scan_target.mkdir()

    result_obj = MagicMock()
    result_obj.score = 90
    result_obj.summary.return_value = "Looks good"
    result_obj.files_scanned = 3
    result_obj.findings = []
    result_obj.blocked = False

    mock_scanner = MagicMock()
    mock_scanner.scan_path.return_value = result_obj
    monkeypatch.setattr("pincer.skills.scanner.PackageScanner", lambda: mock_scanner)

    result = runner.invoke(app, ["mcp", "scan", str(scan_target)])

    assert result.exit_code == 0
    assert "Safe to install" in result.output


def test_mcp_scan_local_path_with_findings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    scan_target = tmp_path / "risky"
    scan_target.mkdir()

    finding = MagicMock()
    finding.risk = "high"
    finding.rule = "eval-usage"
    finding.message = "Uses eval()"
    finding.file = "index.js"
    finding.line = 42

    result_obj = MagicMock()
    result_obj.score = 50
    result_obj.summary.return_value = "Needs review"
    result_obj.files_scanned = 5
    result_obj.findings = [finding]
    result_obj.blocked = False

    mock_scanner = MagicMock()
    mock_scanner.scan_path.return_value = result_obj
    monkeypatch.setattr("pincer.skills.scanner.PackageScanner", lambda: mock_scanner)

    result = runner.invoke(app, ["mcp", "scan", str(scan_target)])

    assert result.exit_code == 0
    assert "eval-usage" in result.output
    assert "review findings" in result.output


def test_mcp_scan_package_name_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    entry = MagicMock()

    result_obj = MagicMock()
    result_obj.score = 30
    result_obj.summary.return_value = "Risky"
    result_obj.files_scanned = 2
    result_obj.findings = []
    result_obj.blocked = True

    mock_client = MagicMock()
    mock_client._scan_package = AsyncMock(return_value=result_obj)
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)
    monkeypatch.setattr("pincer.mcp.registry_client._infer_entry", lambda pkg: entry)

    result = runner.invoke(app, ["mcp", "scan", "some-nonexistent-package-xyz"])

    assert result.exit_code == 0
    assert "BLOCKED" in result.output


# ── mcp uninstall ─────────────────────────────────────────────────────────


def test_mcp_uninstall_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "uninstall", "github"], input="n\n")

    assert result.exit_code == 0
    assert "Cancelled." in result.output
    mock_client.uninstall.assert_not_called()


def test_mcp_uninstall_success_with_yes_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.uninstall.return_value = True
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "uninstall", "github", "--yes"])

    assert result.exit_code == 0
    assert "Removed 'github'" in result.output


def test_mcp_uninstall_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.uninstall.return_value = False
    monkeypatch.setattr("pincer.mcp.registry_client.MCPRegistryClient", lambda: mock_client)

    result = runner.invoke(app, ["mcp", "uninstall", "missing", "--yes"])

    assert result.exit_code == 1
    assert "not found in pincer.toml" in result.output


# ── mcp serve ─────────────────────────────────────────────────────────────


def test_mcp_serve_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = False
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 1
    assert "not enabled" in result.output


def test_mcp_serve_invalid_approval_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = True
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "serve", "--approval", "bogus"])

    assert result.exit_code == 1
    assert "Unknown approval mode" in result.output


def test_mcp_serve_webhook_missing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = True
    mock_cfg.server.approval_policy = None
    mock_cfg.server.webhook_url = None
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "serve", "--approval", "webhook"])

    assert result.exit_code == 1
    assert "requires mcp.server.webhook_url" in result.output


def test_mcp_serve_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = True
    mock_cfg.server.approval_policy = None
    mock_cfg.server.webhook_url = None
    mock_cfg.server.host = "127.0.0.1"
    mock_cfg.server.port = 8090
    mock_cfg.server.path = "/mcp"
    mock_cfg.server.expose_tools = ["shell_exec"]
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_shell = MagicMock()
    mock_shell.run = AsyncMock()
    monkeypatch.setattr("pincer.mcp.standalone.StandaloneMCPShell", lambda **kwargs: mock_shell)

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 0
    assert "Starting Pincer MCP server" in result.output
    mock_shell.run.assert_awaited_once()


def test_mcp_serve_handles_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = True
    mock_cfg.server.approval_policy = None
    mock_cfg.server.webhook_url = None
    mock_cfg.server.host = "127.0.0.1"
    mock_cfg.server.port = 8090
    mock_cfg.server.path = "/mcp"
    mock_cfg.server.expose_tools = []
    mock_cfg.servers = []
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    mock_shell = MagicMock()
    mock_shell.run = AsyncMock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("pincer.mcp.standalone.StandaloneMCPShell", lambda **kwargs: mock_shell)

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 0
    assert "Stopped." in result.output
