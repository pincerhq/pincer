"""Tests for MCP configuration parsing and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pincer.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    MCPTransport,
    _interpolate_env,
    load_mcp_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── MCPServerConfig validation ──────────────────────────────────────────────


def test_stdio_requires_command() -> None:
    with pytest.raises(ValueError, match="command"):
        MCPServerConfig(name="test", transport=MCPTransport.STDIO)


def test_http_requires_url() -> None:
    with pytest.raises(ValueError, match="url"):
        MCPServerConfig(name="test", transport=MCPTransport.STREAMABLE_HTTP)


def test_invalid_name_rejected() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        MCPServerConfig(name="bad-name", transport=MCPTransport.STDIO, command="npx")


def test_valid_stdio_config() -> None:
    cfg = MCPServerConfig(
        name="github",
        transport=MCPTransport.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
    )
    assert cfg.name == "github"
    assert cfg.sandbox is True
    assert cfg.approval_required == ["*"]


def test_valid_http_config() -> None:
    cfg = MCPServerConfig(
        name="postgres",
        transport=MCPTransport.STREAMABLE_HTTP,
        url="https://example.com/mcp",
        approval_required=["execute_query"],
    )
    assert cfg.url == "https://example.com/mcp"


# ── MCPConfig validation ─────────────────────────────────────────────────────


def test_duplicate_names_rejected() -> None:
    srv = MCPServerConfig(name="dup", transport=MCPTransport.STDIO, command="echo")
    with pytest.raises(ValueError, match="Duplicate"):
        MCPConfig(servers=[srv, srv])


def test_too_many_servers_rejected() -> None:
    servers = [MCPServerConfig(name=f"s{i}", transport=MCPTransport.STDIO, command="echo") for i in range(12)]
    with pytest.raises(ValueError, match="Too many"):
        MCPConfig(max_servers=10, servers=servers)


def test_empty_config_is_valid() -> None:
    cfg = MCPConfig()
    assert cfg.enabled is True
    assert cfg.servers == []


# ── Environment variable interpolation ───────────────────────────────────────


def test_env_interpolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    result = _interpolate_env("Bearer ${GITHUB_TOKEN}")
    assert result == "Bearer ghp_test123"


def test_env_interpolation_missing_var() -> None:
    result = _interpolate_env("${NONEXISTENT_VAR_12345}")
    assert result == "${NONEXISTENT_VAR_12345}"  # unresolved refs pass through


def test_env_interpolation_no_vars() -> None:
    result = _interpolate_env("plain string")
    assert result == "plain string"


# ── TOML loading ─────────────────────────────────────────────────────────────


def test_load_mcp_config_no_file(tmp_path: Path) -> None:
    """Returns empty config when no pincer.toml exists."""
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.servers == []


def test_load_mcp_config_from_toml(tmp_path: Path) -> None:
    toml_content = """
[mcp]
enabled = true
tool_prefix = true

[[mcp.servers]]
name = "github"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
approval_required = ["*"]
timeout = 30
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is True
    assert len(cfg.servers) == 1
    assert cfg.servers[0].name == "github"
    assert cfg.servers[0].transport == MCPTransport.STDIO
    assert cfg.servers[0].command == "npx"


def test_load_mcp_config_disabled(tmp_path: Path) -> None:
    toml_content = """
[mcp]
enabled = false
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is False


def test_load_mcp_config_env_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "testserver")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "echo")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_ARGS", "hello,world")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_APPROVAL", "*")

    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    srv = cfg.servers[0]
    assert srv.name == "testserver"
    assert srv.command == "echo"
    assert srv.args == ["hello", "world"]


def test_mcp_disabled_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MCP_ENABLED", "false")
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is False


def test_env_server_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-defined server with same name replaces TOML server."""
    toml_content = """
[mcp]
[[mcp.servers]]
name = "myserver"
transport = "stdio"
command = "original_command"
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "myserver")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "overridden_command")

    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].command == "overridden_command"


# ── [mcp] enabled = false — skills gate ─────────────────────────────────────


def test_mcp_disabled_servers_not_parsed(tmp_path: Path) -> None:
    """When enabled=false, servers in the TOML are not parsed and not returned."""
    toml_content = """
[mcp]
enabled = false

[[mcp.servers]]
name = "myserver"
transport = "stdio"
command = "python"
args = ["server.py"]
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is False
    assert cfg.servers == []


def test_mcp_disabled_invalid_server_no_exception(tmp_path: Path) -> None:
    """enabled=false short-circuits before server validation — no ValueError raised."""
    # A stdio server without 'command' would fail MCPServerConfig.__post_init__
    # if parsed, but the early return must fire before that.
    toml_content = """
[mcp]
enabled = false

[[mcp.servers]]
name = "broken"
transport = "stdio"
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)  # must not raise
    assert cfg.enabled is False


def test_mcp_disabled_does_not_block_skills(tmp_path: Path) -> None:
    """The _mcp_active gate in cli.py must be False when enabled=false."""
    toml_content = """
[mcp]
enabled = false

[[mcp.servers]]
name = "myserver"
transport = "stdio"
command = "python"
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    # Reproduce the exact condition used in cli._run_agent
    mcp_active = cfg is not None and cfg.enabled is True and bool(cfg.servers)
    assert not mcp_active, "Skills must not be blocked when [mcp] enabled = false"


def test_mcp_enabled_no_servers_does_not_block_skills(tmp_path: Path) -> None:
    """enabled=true but no servers: _mcp_active is False, skills load."""
    toml_content = """
[mcp]
enabled = true
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    mcp_active = cfg is not None and cfg.enabled is True and bool(cfg.servers)
    assert not mcp_active


def test_mcp_active_when_enabled_with_servers(tmp_path: Path) -> None:
    """enabled=true with servers: _mcp_active is True, skills are skipped."""
    toml_content = """
[mcp]
enabled = true

[[mcp.servers]]
name = "myserver"
transport = "stdio"
command = "python"
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    mcp_active = cfg is not None and cfg.enabled is True and bool(cfg.servers)
    assert mcp_active


def test_mcp_disabled_via_env_does_not_block_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PINCER_MCP_ENABLED=false must also result in _mcp_active=False."""
    monkeypatch.setenv("PINCER_MCP_ENABLED", "false")
    # Even if servers are defined via env vars, the global toggle wins
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "srv")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    cfg = load_mcp_config(tmp_path)
    assert cfg.enabled is False
    mcp_active = cfg is not None and cfg.enabled is True and bool(cfg.servers)
    assert not mcp_active


def test_env_var_in_toml_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret123")
    toml_content = """
[mcp]
[[mcp.servers]]
name = "myserver"
transport = "streamable-http"
url = "https://example.com/mcp"
[mcp.servers.headers]
Authorization = "Bearer ${MY_TOKEN}"
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].headers["Authorization"] == "Bearer secret123"
