"""Which telemetry provider the agent exports to, from its typed settings.

The agent reads `PINCER_TELEMETRY_UPTRACE_DSN` / `PINCER_TELEMETRY_POSTHOG_DSN`
(settings `telemetry_uptrace_dsn` / `telemetry_posthog_dsn`). The rules — at
most one provider, a well-formed DSN — live in pincer-telemetry's
`provider_from_prefixed_env`, which the bundled MCP servers call for their
`TELEMETRY_*_DSN` variables too; this module only feeds it the settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

if TYPE_CHECKING:
    from pincer_telemetry import Provider

    from pincer.config import Settings

ENV_PREFIX = "PINCER_TELEMETRY_"
UPTRACE_ENV = f"{ENV_PREFIX}UPTRACE_DSN"
POSTHOG_ENV = f"{ENV_PREFIX}POSTHOG_DSN"
LEGACY_DSN_ENV = f"{ENV_PREFIX}DSN"
_LEGACY_DSN_TOML_KEY = "telemetry_dsn"

_FIELDS = {UPTRACE_ENV: "telemetry_uptrace_dsn", POSTHOG_ENV: "telemetry_posthog_dsn"}


def _telemetry_env(settings: Settings) -> dict[str, str]:
    """The non-empty telemetry DSN settings, keyed by their environment variable names."""
    env: dict[str, str] = {}
    for key, field in _FIELDS.items():
        value = getattr(settings, field, None)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str) and value.strip():
            env[key] = value.strip()
    return env


def telemetry_configured(settings: Settings) -> bool:
    """True when any telemetry DSN is set, valid or not."""
    return bool(_telemetry_env(settings))


def resolve_telemetry_provider(settings: Settings) -> Provider | None:
    """The provider the DSN settings select; None when none is set.

    Raises `pincer_telemetry.TelemetryConfigError` when both are set or the DSN
    is malformed, and ImportError when the `telemetry` extra is not installed.
    """
    from pincer_telemetry import provider_from_prefixed_env

    return provider_from_prefixed_env(ENV_PREFIX, _telemetry_env(settings))


def legacy_telemetry_dsn(config_dir: Path | None = None) -> str | None:
    """Where the removed `PINCER_TELEMETRY_DSN` is still set, if anywhere; None otherwise.

    Looks in the environment, the `.env` files Settings reads, and a top-level
    `telemetry_dsn` in pincer.toml / pincer.local.toml. Returns a description of
    the location, never the value (the DSN carries a token).
    """
    for key, value in os.environ.items():
        if key.upper() == LEGACY_DSN_ENV and value.strip():
            return f"{LEGACY_DSN_ENV} in the environment"

    from dotenv import dotenv_values

    from pincer.config.main import Settings
    from pincer.config.toml_loader import read_toml_raw

    base_dir = config_dir or Path.cwd()
    env_files = Settings.model_config.get("env_file") or ()
    if isinstance(env_files, str | os.PathLike):
        env_files = (env_files,)
    for env_file in env_files:
        path = base_dir / env_file
        if not path.is_file():
            continue
        for key, env_value in dotenv_values(path).items():
            if key.upper() == LEGACY_DSN_ENV and env_value and env_value.strip():
                return f"{LEGACY_DSN_ENV} in {path}"

    for name in ("pincer.toml", "pincer.local.toml"):
        raw = read_toml_raw(base_dir / name) or {}
        if raw.get(_LEGACY_DSN_TOML_KEY):
            return f"{_LEGACY_DSN_TOML_KEY} in {base_dir / name}"
    return None


def legacy_telemetry_dsn_error(location: str) -> str:
    """The message for a leftover `PINCER_TELEMETRY_DSN` / `telemetry_dsn`."""
    return (
        f"{location} is no longer read. Rename it to {UPTRACE_ENV} for Uptrace "
        f"(DSN https://<token>@api.uptrace.dev?grpc=4317), or use {POSTHOG_ENV} for PostHog "
        "(DSN https://<phc_api_key>@us.i.posthog.com). Set at most one of them."
    )
