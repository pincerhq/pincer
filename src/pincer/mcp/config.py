"""
MCP server configuration — loaded from pincer.toml, pincer.local.toml, and/or environment variables.

Priority (highest to lowest):
1. Environment variable overrides (PINCER_MCP_SERVER_N_*)
2. pincer.local.toml  (optional — overrides / extends pincer.toml)
3. pincer.toml
4. Built-in defaults

pincer.local.toml supports all sections that pincer.toml supports.
[[mcp.servers]] entries are merged by the 'name' field — a local entry with the
same name as a base entry is merged into it field by field (override wins per
key, unspecified fields are inherited from the base entry); new names are
appended.
Add pincer.local.toml to .gitignore to keep machine-local overrides out of VCS.

Environment variable format for a single server:
    PINCER_MCP_ENABLED=true
    PINCER_MCP_SERVER_1_NAME=github
    PINCER_MCP_SERVER_1_TRANSPORT=stdio
    PINCER_MCP_SERVER_1_COMMAND=npx
    PINCER_MCP_SERVER_1_ARGS=-y,@modelcontextprotocol/server-github
    PINCER_MCP_SERVER_1_ENV_GITHUB_TOKEN=ghp_xxxxx
    PINCER_MCP_SERVER_1_APPROVAL=*
    PINCER_MCP_SERVER_1_TIMEOUT=30
    PINCER_MCP_SERVER_1_STARTUP_TIMEOUT=30
    PINCER_MCP_SERVER_1_SANDBOX=true
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.config.toml_loader import read_toml_raw as _read_toml_raw

if TYPE_CHECKING:
    from pincer.config.main import Settings

# Mirrors Settings.model_config["env_file"] in pincer/config/main.py: ".env" takes
# priority over "../.env" (monorepo layout — commands run from a subdirectory
# still find the repo-root .env as a fallback).
_DOTENV_CANDIDATES = (".env", "../.env")


def _load_dotenv_into_environ() -> None:
    """Load .env into the real process environment (os.environ), not just pydantic-settings.

    MCP servers are external processes with their own env vars (e.g. an MCP
    server's client id/secret) that Pincer core has no business knowing by
    name — pincer.toml's ``${VAR}`` interpolation resolves those straight
    against os.environ (see `_interpolate_env`). That fallback is a no-op
    unless .env actually lands in os.environ, since pydantic-settings' own
    dotenv parsing only populates its own declared Settings fields.
    """
    from dotenv import load_dotenv

    for candidate in _DOTENV_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            load_dotenv(path, override=False)


class MCPTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server connection."""

    name: str
    transport: MCPTransport
    enabled: bool = True

    # stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # streamable-http transport
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    # Security & behavior
    sandbox: bool = True
    sandbox_memory_mb: int = 0  # 0 = no RLIMIT_AS; set explicitly only for known-safe binaries
    approval_required: list[str] = field(default_factory=lambda: ["*"])
    timeout: int = 30
    startup_timeout: int = 30
    max_retries: int = 2

    # OAuth 2.1 client-side settings (for HTTP servers that require auth)
    oauth_enabled: bool = False
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None

    def __post_init__(self) -> None:
        if self.transport == MCPTransport.STDIO and not self.command:
            raise ValueError(f"MCP server '{self.name}': stdio transport requires 'command'")
        if self.transport == MCPTransport.STREAMABLE_HTTP and not self.url:
            raise ValueError(f"MCP server '{self.name}': streamable-http transport requires 'url'")
        if not re.match(r"^[a-zA-Z0-9_]+$", self.name):
            raise ValueError(f"MCP server name must be alphanumeric/underscore: '{self.name}'")


@dataclass(frozen=True)
class MCPApprovalPolicyConfig:
    """Approval policy for standalone MCP server mode (no messaging channels)."""

    read: str = "approve"  # auto-approve read-only tools
    write: str = "deny"  # auto-deny write tools
    destructive: str = "deny"  # auto-deny destructive tools
    unknown: str = "deny"  # auto-deny unknown risk tools
    default: str = "deny"  # default for any unlisted risk level

    def as_dict(self) -> dict[str, str]:
        return {
            "read": self.read,
            "write": self.write,
            "destructive": self.destructive,
            "unknown": self.unknown,
            "default": self.default,
        }


@dataclass(frozen=True)
class MCPSamplingConfig:
    """Sampling configuration — allows MCP server to handle LLM inference requests."""

    enabled: bool = False
    model: str = "claude-sonnet-4-20250514"
    budget_per_day: float = 5.00
    max_tokens: int = 1024


@dataclass(frozen=True)
class MCPServerExportConfig:
    """Config for Pincer's outbound MCP server (exposes Pincer tools to MCP clients)."""

    enabled: bool = False  # Disabled by default — must explicitly opt in
    host: str = "127.0.0.1"  # Localhost-only unless explicitly changed
    port: int = 18800
    path: str = "/mcp"
    # Which Pincer tools to expose (conservative default: read-only + ask_user)
    expose_tools: list[str] = field(
        default_factory=lambda: [
            "email_check",
            "calendar_today",
            "memory_search",
            "load_skill",
            "load_skill_reference",
        ]
    )
    # Approval policy for standalone mode
    approval_policy: MCPApprovalPolicyConfig = field(default_factory=MCPApprovalPolicyConfig)
    # Sampling configuration
    sampling: MCPSamplingConfig = field(default_factory=MCPSamplingConfig)
    # Webhook URL for webhook approval mode
    webhook_url: str | None = None
    # OAuth 2.1 auth config (None = disabled, uses localhost bypass)
    auth: Any | None = None


@dataclass(frozen=True)
class MCPConfig:
    """Top-level MCP configuration."""

    enabled: bool = True
    max_servers: int = 10
    tool_prefix: bool = True
    servers: list[MCPServerConfig] = field(default_factory=list)
    server: MCPServerExportConfig = field(default_factory=MCPServerExportConfig)

    def __post_init__(self) -> None:
        enabled_servers = [s for s in self.servers if s.enabled]
        if len(enabled_servers) > self.max_servers:
            raise ValueError(f"Too many enabled MCP servers ({len(enabled_servers)} > {self.max_servers})")
        names = [s.name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate MCP server names detected")


def _pincer_config_vars(settings: Settings) -> dict[str, str]:
    """Build an explicit PINCER_* → value mapping from a Settings instance."""
    from pydantic import SecretStr

    from pincer.config.channels import ChannelSettings
    from pincer.config.tools import ToolSettings

    def _str(v: object) -> str:
        if isinstance(v, SecretStr):
            return v.get_secret_value()
        return str(v)

    result: dict[str, str] = {
        "PINCER_DATA_DIR": _str(settings.data_dir),
        "PINCER_LOG_LEVEL": _str(settings.log_level),
        "PINCER_TIMEZONE": _str(settings.timezone),
    }
    for field_name in ToolSettings.model_fields:
        if field_name.startswith("shell_"):
            result[f"PINCER_{field_name.upper()}"] = _str(getattr(settings, field_name))
    for field_name in ChannelSettings.model_fields:
        if field_name.startswith("email_"):
            result[f"PINCER_{field_name.upper()}"] = _str(getattr(settings, field_name))

    from pincer.config.mcp import MCPSettings

    for field_name in MCPSettings.model_fields:
        result[f"PINCER_{field_name.upper()}"] = _str(getattr(settings, field_name))

    return result


def _interpolate_env(value: str, extra: dict[str, str] | None = None) -> str:
    """Resolve ${VAR} and ${VAR:-$FALLBACK} references.

    Lookup order when extra is provided: extra (Pincer config) → os.environ → "".
    When extra is None (legacy callers): unresolved placeholders pass through unchanged.
    """

    def _replace(m: re.Match[str]) -> str:
        var, fallback_var = m.group(1), m.group(2)
        if extra is not None and var in extra:
            return extra[var]
        if var in os.environ:
            return os.environ[var]
        if fallback_var is not None:
            if extra is not None and fallback_var in extra:
                return extra[fallback_var]
            return os.environ.get(fallback_var, "" if extra is not None else m.group(0))
        return "" if extra is not None else m.group(0)

    return re.sub(r"\$\{([^}:]+)(?::-\$([^}]+))?\}", _replace, value)


def _interpolate_dict(d: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    return {k: _interpolate_env(v, extra) for k, v in d.items()}


def _interpolate_list(values: list[str], extra: dict[str, str] | None = None) -> list[str]:
    return [_interpolate_env(v, extra) for v in values]


def _parse_server_from_toml(raw: dict[str, Any], pincer_vars: dict[str, str] | None = None) -> MCPServerConfig:
    """Parse a single [[mcp.servers]] TOML entry."""
    transport = MCPTransport(raw.get("transport", "stdio"))
    env = _interpolate_dict(raw.get("env", {}), pincer_vars)
    headers = _interpolate_dict(raw.get("headers", {}), pincer_vars)
    approval = raw.get("approval_required", ["*"])
    if isinstance(approval, str):
        approval = [approval]

    return MCPServerConfig(
        name=raw["name"],
        transport=transport,
        enabled=raw.get("enabled", True),
        command=raw.get("command"),
        args=_interpolate_list(raw.get("args", []), pincer_vars),
        env=env,
        url=raw.get("url"),
        headers=headers,
        sandbox=raw.get("sandbox", True),
        sandbox_memory_mb=int(raw.get("sandbox_memory_mb", 0)),
        approval_required=approval,
        timeout=int(raw.get("timeout", 30)),
        startup_timeout=int(raw.get("startup_timeout", 30)),
        max_retries=int(raw.get("max_retries", 2)),
    )


_STDIO_ONLY_KEYS = {"command", "args", "env"}
_STREAMABLE_HTTP_ONLY_KEYS = {"url", "headers"}


def _merge_mcp_raw(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge two raw [mcp] section dicts. override wins for all scalar/mapping keys.

    [[mcp.servers]] entries are merged by 'name': an override entry with the same
    name as a base entry is merged into it field by field (override wins per key,
    unspecified fields are inherited from the base entry); new names are appended
    in order. [mcp.server] sub-table fields are merged the same way. If an override
    entry switches 'transport', the previous transport's exclusive fields (e.g.
    stdio's 'command'/'args'/'env' vs streamable-http's 'url'/'headers') are dropped
    from the base entry instead of being inherited.
    """
    merged: dict[str, Any] = dict(base)
    for key, val in override.items():
        if key == "servers":
            by_name: dict[str, Any] = {s["name"]: s for s in base.get("servers", [])}
            for srv in val:
                name = srv["name"]
                if name in by_name:
                    base_srv = by_name[name]
                    if "transport" in srv and srv["transport"] != base_srv.get("transport"):
                        drop = _STREAMABLE_HTTP_ONLY_KEYS if srv["transport"] == "stdio" else _STDIO_ONLY_KEYS
                        base_srv = {k: v for k, v in base_srv.items() if k not in drop}
                    by_name[name] = {**base_srv, **srv}
                else:
                    by_name[name] = srv
            merged["servers"] = list(by_name.values())
        elif key == "server" and isinstance(val, dict):
            merged["server"] = {**base.get("server", {}), **val}
        else:
            merged[key] = val
    return merged


def _parse_mcp_config(mcp_raw: dict[str, Any], pincer_vars: dict[str, str] | None = None) -> MCPConfig:
    """Parse a raw [mcp] section dict into an MCPConfig."""
    servers = [_parse_server_from_toml(s, pincer_vars) for s in mcp_raw.get("servers", [])]
    srv_raw = mcp_raw.get("server", {})
    # Parse approval_policy sub-table
    ap_raw = srv_raw.get("approval_policy", {})
    approval_policy = MCPApprovalPolicyConfig(
        read=ap_raw.get("read", "approve"),
        write=ap_raw.get("write", "deny"),
        destructive=ap_raw.get("destructive", "deny"),
        unknown=ap_raw.get("unknown", "deny"),
        default=ap_raw.get("default", "deny"),
    )
    # Parse sampling sub-table
    samp_raw = srv_raw.get("sampling", {})
    sampling = MCPSamplingConfig(
        enabled=samp_raw.get("enabled", False),
        model=samp_raw.get("model", "claude-sonnet-4-20250514"),
        budget_per_day=float(samp_raw.get("budget_per_day", 5.00)),
        max_tokens=int(samp_raw.get("max_tokens", 1024)),
    )
    srv_export = MCPServerExportConfig(
        enabled=srv_raw.get("enabled", False),
        host=srv_raw.get("host", "127.0.0.1"),
        port=int(srv_raw.get("port", 18800)),
        path=srv_raw.get("path", "/mcp"),
        expose_tools=srv_raw.get(
            "expose_tools",
            [
                "email_check",
                "calendar_today",
                "memory_search",
                "load_skill",
                "load_skill_reference",
            ],
        ),
        approval_policy=approval_policy,
        sampling=sampling,
        webhook_url=srv_raw.get("webhook_url"),
    )
    return MCPConfig(
        enabled=mcp_raw.get("enabled", True),
        max_servers=int(mcp_raw.get("max_servers", 10)),
        tool_prefix=mcp_raw.get("tool_prefix", True),
        servers=servers,
        server=srv_export,
    )


def _load_env_servers(pincer_vars: dict[str, str] | None = None) -> list[MCPServerConfig]:
    """Build server configs from PINCER_MCP_SERVER_N_* env vars."""
    servers: list[MCPServerConfig] = []
    # Find all defined server indices: PINCER_MCP_SERVER_1_NAME, PINCER_MCP_SERVER_2_NAME, ...
    indices: set[str] = set()
    for key in os.environ:
        m = re.match(r"^PINCER_MCP_SERVER_(\d+)_NAME$", key, re.IGNORECASE)
        if m:
            indices.add(m.group(1))

    for idx in sorted(indices):
        prefix = f"PINCER_MCP_SERVER_{idx}_"
        name = os.environ.get(f"{prefix}NAME", "")
        if not name:
            continue
        transport_str = os.environ.get(f"{prefix}TRANSPORT", "stdio")
        try:
            transport = MCPTransport(transport_str)
        except ValueError:
            continue

        # Parse args: comma-separated
        args_str = os.environ.get(f"{prefix}ARGS", "")
        args = _interpolate_list([a.strip() for a in args_str.split(",") if a.strip()], pincer_vars) if args_str else []

        # Parse env: PINCER_MCP_SERVER_N_ENV_VARNAME=value
        env: dict[str, str] = {}
        env_prefix = f"{prefix}ENV_"
        for key, val in os.environ.items():
            if key.upper().startswith(env_prefix):
                env_key = key[len(env_prefix) :]
                env[env_key] = _interpolate_env(val, pincer_vars)

        # Approval patterns
        approval_str = os.environ.get(f"{prefix}APPROVAL", "*")
        approval = [a.strip() for a in approval_str.split(",") if a.strip()]

        enabled_str = os.environ.get(f"{prefix}ENABLED", "true").lower()
        sandbox_str = os.environ.get(f"{prefix}SANDBOX", "true").lower()

        try:
            servers.append(
                MCPServerConfig(
                    name=name,
                    transport=transport,
                    enabled=enabled_str in ("true", "1", "yes"),
                    command=os.environ.get(f"{prefix}COMMAND"),
                    args=args,
                    env=env,
                    url=os.environ.get(f"{prefix}URL"),
                    headers={},
                    sandbox=sandbox_str in ("true", "1", "yes"),
                    sandbox_memory_mb=int(os.environ.get(f"{prefix}SANDBOX_MEMORY_MB", "0")),
                    approval_required=approval,
                    timeout=int(os.environ.get(f"{prefix}TIMEOUT", "30")),
                    startup_timeout=int(os.environ.get(f"{prefix}STARTUP_TIMEOUT", "30")),
                    max_retries=int(os.environ.get(f"{prefix}MAX_RETRIES", "2")),
                )
            )
        except (ValueError, TypeError):
            continue

    return servers


def load_mcp_config(config_dir: Path | None = None, pincer_vars: dict[str, str] | None = None) -> MCPConfig:
    """
    Load MCP configuration from pincer.toml, pincer.local.toml, and environment variables.

    Merge order (highest priority last, so it wins):
      pincer.toml  →  pincer.local.toml  →  environment variables

    pincer.local.toml is optional. When present, its [mcp] section is merged on
    top of pincer.toml's [mcp] section before env vars are applied.
    [[mcp.servers]] entries are matched by 'name'; a local entry with the same
    name is merged into the base entry field by field, with unspecified fields
    inherited from the base entry.

    Pass pincer_vars (built from get_settings() via _pincer_config_vars) to resolve
    ${PINCER_*} placeholders in server env/args/headers from Pincer's own config.

    Call this at agent startup; invalid config raises ValueError immediately.
    """
    _load_dotenv_into_environ()
    base_dir = config_dir or Path.cwd()

    # Load raw TOML dicts
    base_raw = _read_toml_raw(base_dir / "pincer.toml")
    local_raw = _read_toml_raw(base_dir / "pincer.local.toml")

    # Merge [mcp] sections from both files
    mcp_raw: dict[str, Any] | None = base_raw.get("mcp") if base_raw else None
    if local_raw:
        local_mcp = local_raw.get("mcp")
        if local_mcp:
            mcp_raw = _merge_mcp_raw(mcp_raw or {}, local_mcp)

    # Global MCP toggle
    enabled_env = os.environ.get("PINCER_MCP_ENABLED", "").lower()
    if enabled_env in ("false", "0", "no"):
        return MCPConfig(enabled=False)
    if mcp_raw and not mcp_raw.get("enabled", True):
        return MCPConfig(enabled=False)

    toml_cfg = _parse_mcp_config(mcp_raw, pincer_vars) if mcp_raw else None

    # Merge: TOML+local servers + env-defined servers
    toml_servers = toml_cfg.servers if toml_cfg else []
    env_servers = _load_env_servers(pincer_vars)

    # Env servers override TOML servers with the same name
    toml_by_name = {s.name: s for s in toml_servers}
    for env_srv in env_servers:
        toml_by_name[env_srv.name] = env_srv
    merged_servers = list(toml_by_name.values())

    max_servers = int(os.environ.get("PINCER_MCP_MAX_SERVERS", toml_cfg.max_servers if toml_cfg else 10))
    tool_prefix_env = os.environ.get("PINCER_MCP_TOOL_PREFIX", "")
    tool_prefix = (
        tool_prefix_env.lower() not in ("false", "0", "no")
        if tool_prefix_env
        else (toml_cfg.tool_prefix if toml_cfg else True)
    )

    # MCP server export config (env overrides TOML)
    toml_srv = toml_cfg.server if toml_cfg else MCPServerExportConfig()
    srv_enabled_env = os.environ.get("PINCER_MCP_SERVER_EXPORT_ENABLED", "").lower()
    srv_export = MCPServerExportConfig(
        enabled=(srv_enabled_env in ("true", "1", "yes") if srv_enabled_env else toml_srv.enabled),
        host=os.environ.get("PINCER_MCP_SERVER_EXPORT_HOST", toml_srv.host),
        port=int(os.environ.get("PINCER_MCP_SERVER_EXPORT_PORT", toml_srv.port)),
        path=os.environ.get("PINCER_MCP_SERVER_EXPORT_PATH", toml_srv.path),
        expose_tools=toml_srv.expose_tools,
        approval_policy=toml_srv.approval_policy,
        sampling=toml_srv.sampling,
        webhook_url=toml_srv.webhook_url,
        auth=toml_srv.auth,
    )

    return MCPConfig(
        enabled=True,
        max_servers=max_servers,
        tool_prefix=tool_prefix,
        servers=merged_servers,
        server=srv_export,
    )
