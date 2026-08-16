"""Tests for MCP configuration parsing and validation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from pincer.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    MCPTransport,
    _interpolate_env,
    _pincer_config_vars,
    load_mcp_config,
)

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


def test_env_interpolation_fallback_primary_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "primary")
    monkeypatch.setenv("MY_FALLBACK", "fallback")
    result = _interpolate_env("${MY_VAR:-$MY_FALLBACK}/path")
    assert result == "primary/path"


def test_env_interpolation_fallback_used_when_primary_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_VAR", raising=False)
    monkeypatch.setenv("MY_FALLBACK", "/resolved")
    result = _interpolate_env("${MY_VAR:-$MY_FALLBACK}/src/server.py")
    assert result == "/resolved/src/server.py"


def test_env_interpolation_fallback_passthrough_when_both_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_VAR", raising=False)
    monkeypatch.delenv("MY_FALLBACK", raising=False)
    result = _interpolate_env("${MY_VAR:-$MY_FALLBACK}/src")
    assert result == "${MY_VAR:-$MY_FALLBACK}/src"


def test_env_interpolation_fallback_primary_takes_precedence_over_set_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "/override")
    monkeypatch.setenv("MY_FALLBACK", "/should-not-appear")
    result = _interpolate_env("${MY_VAR:-$MY_FALLBACK}")
    assert result == "/override"


# ── _pincer_config_vars ───────────────────────────────────────────────────────


def _make_settings(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        data_dir=Path("/tmp/pincer_test"),
        log_level="INFO",
        timezone="UTC",
        shell_enabled=True,
        shell_timeout=30,
        shell_require_approval=True,
        email_imap_host="",
        email_imap_port=993,
        email_smtp_host="",
        email_smtp_port=587,
        email_username="",
        email_password=SecretStr(""),
        email_from="",
        # MCPSettings fields — needed for PINCER_* interpolation vars
        newsapi_key=SecretStr(""),
        openweathermap_api_key=SecretStr(""),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_pincer_config_vars_data_dir() -> None:
    result = _pincer_config_vars(_make_settings(data_dir=Path("/tmp/pincer_test")))
    assert result["PINCER_DATA_DIR"] == "/tmp/pincer_test"


def test_pincer_config_vars_all_fields() -> None:
    from pincer.config.channels import ChannelSettings
    from pincer.config.tools import ToolSettings

    settings = _make_settings(
        data_dir=Path("/tmp/data"),
        log_level="DEBUG",
        timezone="UTC",
        shell_enabled=False,
        shell_timeout=60,
        shell_require_approval=False,
        email_imap_host="imap.example.com",
        email_smtp_host="smtp.example.com",
        email_username="user@example.com",
        email_password=SecretStr("s3cr3t"),
    )
    result = _pincer_config_vars(settings)
    assert result["PINCER_DATA_DIR"] == "/tmp/data"
    assert result["PINCER_LOG_LEVEL"] == "DEBUG"
    assert result["PINCER_TIMEZONE"] == "UTC"
    assert result["PINCER_SHELL_ENABLED"] == "False"
    assert result["PINCER_SHELL_TIMEOUT"] == "60"
    assert result["PINCER_EMAIL_IMAP_HOST"] == "imap.example.com"
    assert result["PINCER_EMAIL_PASSWORD"] == "s3cr3t"
    # All shell_* and email_* fields must be present (mirrors model_fields)
    for field_name in ToolSettings.model_fields:
        if field_name.startswith("shell_"):
            assert f"PINCER_{field_name.upper()}" in result
    for field_name in ChannelSettings.model_fields:
        if field_name.startswith("email_"):
            assert f"PINCER_{field_name.upper()}" in result
    # MCP credential vars Pincer's own tools also consume directly are present
    # for ${} interpolation in server env dicts
    assert "PINCER_OPENWEATHERMAP_API_KEY" in result
    assert "PINCER_NEWSAPI_KEY" in result
    # Non-MCP settings are not included (not settable via MCPSettings)
    assert "PINCER_DAILY_BUDGET_USD" not in result
    assert "PINCER_BRIEFING_TIME" not in result
    assert "PINCER_BRIEFING_TIMEZONE" not in result
    # Credentials that only an external MCP server needs (e.g. MS365 client
    # id/secret/tenant) are deliberately NOT in MCPSettings/pincer_vars — they
    # flow straight from os.environ (see test_env_interpolation_reads_dotenv_var).
    assert "PINCER_MS365_CLIENT_ID" not in result


def test_interpolate_env_extra_wins_over_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_DATA_DIR", "/from/os/environ")
    result = _interpolate_env("${PINCER_DATA_DIR}/db", extra={"PINCER_DATA_DIR": "/from/settings"})
    assert result == "/from/settings/db"


def test_interpolate_env_extra_fallback_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    result = _interpolate_env("Bearer ${GITHUB_TOKEN}", extra={"PINCER_DATA_DIR": "/x"})
    assert result == "Bearer ghp_test"


def test_interpolate_env_unresolved_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NONEXISTENT_VAR_12345", raising=False)
    result = _interpolate_env("${NONEXISTENT_VAR_12345}", extra={})
    assert result == ""


# ── TOML loading ─────────────────────────────────────────────────────────────


def test_fallback_interpolation_in_toml_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINCER_SRC_DIR", raising=False)
    monkeypatch.setenv("PWD", "/home/user/pincer")
    toml_content = """
[mcp]
enabled = true

[[mcp.servers]]
name = "memory"
transport = "stdio"
command = "python"
args = ["${PINCER_SRC_DIR:-$PWD}/src/sqlite-vec-memory-mcp/memory_server.py"]
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].args[0] == "/home/user/pincer/src/sqlite-vec-memory-mcp/memory_server.py"


def test_load_mcp_config_pincer_vars_resolves_toml_env(tmp_path: Path) -> None:
    toml_content = """
[mcp]
enabled = true

[[mcp.servers]]
name = "memory"
transport = "stdio"
command = "python"
env = { MEMORY_DB_PATH = "${PINCER_DATA_DIR}/sqlite_vec.db" }
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path, pincer_vars={"PINCER_DATA_DIR": "/tmp/pincer_test"})
    assert cfg.servers[0].env["MEMORY_DB_PATH"] == "/tmp/pincer_test/sqlite_vec.db"


def test_load_mcp_config_resolves_env_var_straight_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """${VAR} in a server's env block resolves from .env directly — no pincer_vars entry needed.

    This is how MCP-server-only credentials (e.g. an external server's client
    secret) reach the subprocess: Pincer core never declares them by name.
    """
    monkeypatch.delenv("SOME_MCP_SERVER_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOME_MCP_SERVER_TOKEN=from-dotenv\n")
    toml_content = """
[mcp]
enabled = true

[[mcp.servers]]
name = "memory"
transport = "stdio"
command = "python"
env = { TOKEN = "${SOME_MCP_SERVER_TOKEN}" }
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path, pincer_vars={"PINCER_DATA_DIR": "/tmp/pincer_test"})
    assert cfg.servers[0].env["TOKEN"] == "from-dotenv"


def test_load_mcp_config_dotenv_does_not_override_real_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real environment variable always wins over the same key in .env."""
    monkeypatch.setenv("SOME_MCP_SERVER_TOKEN", "from-real-env")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOME_MCP_SERVER_TOKEN=from-dotenv\n")
    toml_content = """
[mcp]
enabled = true

[[mcp.servers]]
name = "memory"
transport = "stdio"
command = "python"
env = { TOKEN = "${SOME_MCP_SERVER_TOKEN}" }
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path, pincer_vars={"PINCER_DATA_DIR": "/tmp/pincer_test"})
    assert cfg.servers[0].env["TOKEN"] == "from-real-env"


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


def test_load_mcp_config_pincer_vars_resolves_env_server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "memory")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_ENV_MEMORY_DB_PATH", "${PINCER_DATA_DIR}/sqlite_vec.db")

    cfg = load_mcp_config(tmp_path, pincer_vars={"PINCER_DATA_DIR": "/tmp/envtest"})
    assert cfg.servers[0].env["MEMORY_DB_PATH"] == "/tmp/envtest/sqlite_vec.db"


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


def test_env_var_in_toml_args_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_SRC_DIR", "/workspace/src")
    monkeypatch.setenv("NEWSAPI_KEY", "abc123")
    toml_content = """
[mcp]
[[mcp.servers]]
name = "newsapi"
transport = "stdio"
command = "python"
args = ["${PINCER_SRC_DIR}/pincer-mcps/newsapi_server.py", "--api-key=${NEWSAPI_KEY}", "${MISSING_VAR}"]
"""
    (tmp_path / "pincer.toml").write_text(toml_content)
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].args == [
        "/workspace/src/pincer-mcps/newsapi_server.py",
        "--api-key=abc123",
        "${MISSING_VAR}",
    ]


def test_env_server_args_interpolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_SCRIPT", "server.py")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "envargs")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_ARGS", "${PINCER_SCRIPT},--mode=test")
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].args == ["server.py", "--mode=test"]


# ── pincer.local.toml merge ───────────────────────────────────────────────────


def test_local_toml_overrides_server(tmp_path: Path) -> None:
    """local.toml server with same name overrides the base entry fields."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "srv"
transport = "stdio"
command = "base_cmd"
""")
    (tmp_path / "pincer.local.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "srv"
transport = "stdio"
command = "local_cmd"
""")
    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].command == "local_cmd"


def test_local_toml_merges_server_fields(tmp_path: Path) -> None:
    """local.toml merges fields into existing server rather than replacing whole entry."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "srv"
transport = "stdio"
command = "base_cmd"
args = ["--flag"]
""")
    (tmp_path / "pincer.local.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "srv"
enabled = false
""")
    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].name == "srv"
    assert cfg.servers[0].command == "base_cmd"
    assert cfg.servers[0].args == ["--flag"]
    assert cfg.servers[0].enabled is False


def test_local_toml_adds_new_server(tmp_path: Path) -> None:
    """local.toml server with a new name is appended."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "s1"
transport = "stdio"
command = "cmd1"
""")
    (tmp_path / "pincer.local.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "s2"
transport = "stdio"
command = "cmd2"
""")
    cfg = load_mcp_config(tmp_path)
    assert {s.name for s in cfg.servers} == {"s1", "s2"}


def test_local_toml_only_no_base(tmp_path: Path) -> None:
    """local.toml works standalone with no pincer.toml present."""
    (tmp_path / "pincer.local.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "local_only"
transport = "stdio"
command = "echo"
""")
    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].name == "local_only"


# ── _merge_mcp_raw internals ─────────────────────────────────────────────────


def test_merge_mcp_raw_server_subtable() -> None:
    """[mcp.server] sub-table is shallowly merged — override wins per key."""
    from pincer.mcp.config import _merge_mcp_raw

    base = {"server": {"host": "127.0.0.1", "port": 18800, "enabled": False}}
    override = {"server": {"port": 9000}}
    merged = _merge_mcp_raw(base, override)
    assert merged["server"]["host"] == "127.0.0.1"
    assert merged["server"]["port"] == 9000
    assert merged["server"]["enabled"] is False


def test_merge_mcp_raw_scalar_override() -> None:
    """Scalar keys in the override simply replace base values."""
    from pincer.mcp.config import _merge_mcp_raw

    base = {"enabled": True, "tool_prefix": True, "max_servers": 10}
    override = {"tool_prefix": False, "max_servers": 5}
    merged = _merge_mcp_raw(base, override)
    assert merged["tool_prefix"] is False
    assert merged["max_servers"] == 5
    assert merged["enabled"] is True  # untouched


# ── MCPApprovalPolicyConfig ───────────────────────────────────────────────────


def test_approval_policy_as_dict() -> None:
    from pincer.mcp.config import MCPApprovalPolicyConfig

    policy = MCPApprovalPolicyConfig(write="approve")
    d = policy.as_dict()
    assert d["read"] == "approve"
    assert d["write"] == "approve"
    assert d["destructive"] == "deny"
    assert d["default"] == "deny"


# ── _parse_mcp_config — server export sub-sections ───────────────────────────


def test_load_mcp_config_server_export_section(tmp_path: Path) -> None:
    """[mcp.server] fields are parsed into MCPServerExportConfig."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[mcp.server]
enabled = true
host = "0.0.0.0"
port = 9999
path = "/api/mcp"
webhook_url = "https://hook.example.com"
""")
    cfg = load_mcp_config(tmp_path)
    assert cfg.server.enabled is True
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9999
    assert cfg.server.path == "/api/mcp"
    assert cfg.server.webhook_url == "https://hook.example.com"


def test_load_mcp_config_approval_policy_section(tmp_path: Path) -> None:
    """[mcp.server.approval_policy] is parsed correctly."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[mcp.server.approval_policy]
read = "approve"
write = "approve"
destructive = "deny"
unknown = "deny"
default = "deny"
""")
    cfg = load_mcp_config(tmp_path)
    assert cfg.server.approval_policy.write == "approve"
    assert cfg.server.approval_policy.destructive == "deny"


def test_load_mcp_config_sampling_section(tmp_path: Path) -> None:
    """[mcp.server.sampling] is parsed correctly."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[mcp.server.sampling]
enabled = true
model = "claude-haiku-4-5"
budget_per_day = 10.0
max_tokens = 2048
""")
    cfg = load_mcp_config(tmp_path)
    assert cfg.server.sampling.enabled is True
    assert cfg.server.sampling.model == "claude-haiku-4-5"
    assert cfg.server.sampling.max_tokens == 2048


def test_approval_required_string_in_toml(tmp_path: Path) -> None:
    """approval_required as a bare string is wrapped in a list."""
    (tmp_path / "pincer.toml").write_text("""
[mcp]
[[mcp.servers]]
name = "srv"
transport = "stdio"
command = "python"
approval_required = "none"
""")
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].approval_required == ["none"]


# ── Environment overrides — tool_prefix and server export ─────────────────────


def test_tool_prefix_env_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MCP_TOOL_PREFIX", "false")
    cfg = load_mcp_config(tmp_path)
    assert cfg.tool_prefix is False


def test_tool_prefix_env_true_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pincer.toml").write_text("""
[mcp]
tool_prefix = false
""")
    monkeypatch.setenv("PINCER_MCP_TOOL_PREFIX", "true")
    cfg = load_mcp_config(tmp_path)
    assert cfg.tool_prefix is True


def test_server_export_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_ENABLED", "true")
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_HOST", "0.0.0.0")
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_PORT", "9999")
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_PATH", "/custom")
    cfg = load_mcp_config(tmp_path)
    assert cfg.server.enabled is True
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9999
    assert cfg.server.path == "/custom"


# ── _load_env_servers — error / skip paths ────────────────────────────────────


def test_env_server_invalid_transport_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised transport string causes the env server to be silently skipped."""
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "badtransport")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "not-a-real-transport")
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers == []


def test_env_server_invalid_name_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server name with dashes fails MCPServerConfig validation and is silently skipped."""
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "bad-server-name")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers == []


def test_env_server_env_vars_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PINCER_MCP_SERVER_N_ENV_* values are collected into the server env dict."""
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "envserver")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_ENV_MY_TOKEN", "secret")
    cfg = load_mcp_config(tmp_path)
    assert cfg.servers[0].env.get("MY_TOKEN") == "secret"


def test_env_server_disabled_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PINCER_MCP_SERVER_N_ENABLED=false marks the server as disabled (still in list)."""
    monkeypatch.setenv("PINCER_MCP_SERVER_1_NAME", "srv")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_TRANSPORT", "stdio")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_COMMAND", "python")
    monkeypatch.setenv("PINCER_MCP_SERVER_1_ENABLED", "false")
    cfg = load_mcp_config(tmp_path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].enabled is False
