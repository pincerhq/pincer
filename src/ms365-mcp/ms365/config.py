"""
Configuration for the Microsoft 365 integration.

Reads ``[integrations.ms365]`` from pincer.toml and environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SERVICES = ["email", "calendar", "onedrive", "todo", "teams", "contacts", "onenote"]


@dataclass
class MS365IntegrationConfig:
    """Parsed configuration for the Microsoft 365 integration."""

    enabled: bool = True
    client_id: str = ""
    tenant_id: str = "common"
    auth_method: str = "device_code"  # "device_code" or "interactive"
    services: list[str] = field(default_factory=lambda: list(_DEFAULT_SERVICES))
    token_cache_path: str = ""  # empty → auto-detect

    def __post_init__(self) -> None:
        if not self.client_id:
            self.client_id = os.environ.get("PINCER_MS365_CLIENT_ID", "")
        if self.tenant_id == "common":
            env_tid = os.environ.get("PINCER_MS365_TENANT_ID", "")
            if env_tid and env_tid != "common":
                self.tenant_id = env_tid


def load_config() -> MS365IntegrationConfig:
    """Load MS365 integration config from pincer.toml if present."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return MS365IntegrationConfig()

    toml_path = Path.cwd() / "pincer.toml"
    if not toml_path.exists():
        return MS365IntegrationConfig()

    try:
        data = tomllib.loads(toml_path.read_text())
    except Exception as exc:
        logger.warning("Could not parse pincer.toml: %s", exc)
        return MS365IntegrationConfig()

    section = data.get("integrations", {}).get("ms365", {})
    # Resolve ${VAR} references in client_id
    client_id = section.get("client_id", "")
    if isinstance(client_id, str) and client_id.startswith("${") and client_id.endswith("}"):
        env_var = client_id[2:-1]
        client_id = os.environ.get(env_var, "")

    return MS365IntegrationConfig(
        enabled=section.get("enabled", True),
        client_id=client_id,
        tenant_id=section.get("tenant_id", "common"),
        auth_method=section.get("auth_method", "device_code"),
        services=section.get("services", list(_DEFAULT_SERVICES)),
        token_cache_path=section.get("token_cache_path", ""),
    )


def resolve_cache_path(cfg: MS365IntegrationConfig) -> Path:
    """Return the token cache path, resolving blanks to ~/.pincer/."""
    if cfg.token_cache_path:
        return Path(os.path.expanduser(cfg.token_cache_path))
    return Path.home() / ".pincer" / "ms365_token_cache.json"
