"""Configuration helpers for the Slack integration.

Reads ``[integrations.slack]`` from pincer.toml (or falls back to
environment variables / token file) and returns a SlackIntegrationConfig.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_env(val: str) -> str:
    """Resolve ``${ENV_VAR}`` references in config values."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), val)


@dataclass
class SlackIntegrationConfig:
    """Parsed configuration for the Slack integration."""

    enabled: bool = True
    bot_token: str = ""
    user_token: str = ""


def load_config() -> SlackIntegrationConfig:
    """Load Slack integration config from pincer.toml if present."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return SlackIntegrationConfig()

    toml_path = Path.cwd() / "pincer.toml"
    if not toml_path.exists():
        return SlackIntegrationConfig()

    try:
        data = tomllib.loads(toml_path.read_text())
    except Exception as exc:
        logger.warning("Could not parse pincer.toml: %s", exc)
        return SlackIntegrationConfig()

    section = data.get("integrations", {}).get("slack", {})
    return SlackIntegrationConfig(
        enabled=section.get("enabled", True),
        bot_token=_resolve_env(section.get("bot_token", "")),
        user_token=_resolve_env(section.get("user_token", "")),
    )
