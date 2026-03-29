"""
Configuration helpers for the Google Workspace integration.

Reads ``[integrations.google]`` from pincer.toml (or falls back to
environment variables / defaults) and builds the GoogleAuth + factory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SERVICES = ["gmail", "calendar", "drive", "docs", "sheets", "slides", "tasks", "contacts", "meet"]


@dataclass
class GoogleIntegrationConfig:
    """Parsed configuration for the Google Workspace integration."""

    enabled: bool = True
    services: list[str] = field(default_factory=lambda: list(_DEFAULT_SERVICES))
    credentials_path: str = ""   # empty → auto-detect via settings.google_oauth_dir()
    token_path: str = ""         # empty → auto-detect


def load_config() -> GoogleIntegrationConfig:
    """Load Google integration config from pincer.toml if present."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return GoogleIntegrationConfig()

    toml_path = Path.cwd() / "pincer.toml"
    if not toml_path.exists():
        return GoogleIntegrationConfig()

    try:
        data = tomllib.loads(toml_path.read_text())
    except Exception as exc:
        logger.warning("Could not parse pincer.toml: %s", exc)
        return GoogleIntegrationConfig()

    section = data.get("integrations", {}).get("google", {})
    return GoogleIntegrationConfig(
        enabled=section.get("enabled", True),
        services=section.get("services", list(_DEFAULT_SERVICES)),
        credentials_path=section.get("credentials_path", ""),
        token_path=section.get("token_path", ""),
    )


def resolve_paths(cfg: GoogleIntegrationConfig) -> tuple[Path, Path]:
    """Return (credentials_path, token_path) resolving blanks via settings."""
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()

    credentials_path = Path(cfg.credentials_path) if cfg.credentials_path else oauth_dir / "google_credentials.json"
    token_path = Path(cfg.token_path) if cfg.token_path else oauth_dir / "google_workspace_token.json"
    return credentials_path, token_path
