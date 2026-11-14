"""Which telemetry provider the agent exports to, from its typed settings.

The agent reads `PINCER_TELEMETRY_UPTRACE_DSN` / `PINCER_TELEMETRY_POSTHOG_DSN`
(settings `telemetry_uptrace_dsn` / `telemetry_posthog_dsn`, also settable in
the `[telemetry]` section of pincer.toml). The rules — at most one provider, a
well-formed DSN — live in pincer-telemetry's `provider_from_prefixed_env`,
which the bundled MCP servers call for their `TELEMETRY_*_DSN` variables too;
this module only feeds it the settings.

`inspect_telemetry` is the one place that classifies the configuration;
`pincer run` turns its result into a start or a refusal, `pincer doctor` into
a check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pincer_telemetry import Provider

    from pincer.config import Settings
    from pincer.config.legacy_env import SettingLocation

ENV_PREFIX = "PINCER_TELEMETRY_"
UPTRACE_ENV = f"{ENV_PREFIX}UPTRACE_DSN"
POSTHOG_ENV = f"{ENV_PREFIX}POSTHOG_DSN"
LEGACY_DSN_ENV = f"{ENV_PREFIX}DSN"

#: Environment variable → settings field, for the variables the library validates.
_DSN_SETTING_BY_ENV = {UPTRACE_ENV: "telemetry_uptrace_dsn", POSTHOG_ENV: "telemetry_posthog_dsn"}


class TelemetryState(StrEnum):
    """How the telemetry configuration classifies (see `inspect_telemetry`)."""

    OFF = "off"
    ENABLED = "enabled"
    NOT_INSTALLED = "not_installed"
    LEGACY = "legacy"
    INVALID = "invalid"


@dataclass(frozen=True)
class TelemetrySetup:
    """The outcome of `inspect_telemetry`.

    Attributes:
        state: The classification.
        provider: The provider to start, set only for `ENABLED`.
        message: Why, for `LEGACY` and `INVALID`. Names keys, never values.
    """

    state: TelemetryState
    provider: Provider | None = None
    message: str = ""


def _telemetry_env(settings: Settings) -> dict[str, str]:
    """The non-empty telemetry DSN settings, keyed by their environment variable names."""
    env: dict[str, str] = {}
    for key, setting in _DSN_SETTING_BY_ENV.items():
        dsn = getattr(settings, setting)
        value = dsn.get_secret_value().strip() if dsn is not None else ""
        if value:
            env[key] = value
    return env


def telemetry_configured(settings: Settings) -> bool:
    """Whether any telemetry DSN is set, valid or not.

    Args:
        settings: The agent settings.

    Returns:
        True when `telemetry_uptrace_dsn` or `telemetry_posthog_dsn` is non-empty.
    """
    return bool(_telemetry_env(settings))


def resolve_telemetry_provider(settings: Settings) -> Provider | None:
    """The provider the DSN settings select.

    Args:
        settings: The agent settings.

    Returns:
        The provider, or None when no DSN is set.

    Raises:
        pincer_telemetry.TelemetryConfigError: Both DSNs are set, or the DSN is malformed.
        ImportError: The `telemetry` extra is not installed.
    """
    from pincer_telemetry import provider_from_prefixed_env

    return provider_from_prefixed_env(ENV_PREFIX, _telemetry_env(settings))


def legacy_telemetry_dsn(config_dir: Path | None = None) -> SettingLocation | None:
    """Where the removed telemetry DSN setting is still configured, if anywhere.

    Args:
        config_dir: Where the `.env` and TOML files are looked up. Defaults to the cwd.

    Returns:
        The first of `PINCER_TELEMETRY_DSN` in the environment or a `.env` file
        Settings reads, or `telemetry_dsn` / `[telemetry] dsn` in pincer.toml /
        pincer.local.toml; None when none is set.
    """
    from pincer.config.legacy_env import find_env_setting
    from pincer.config.telemetry import legacy_telemetry_toml

    return find_env_setting(LEGACY_DSN_ENV, config_dir) or legacy_telemetry_toml(config_dir)


def _legacy_message(location: SettingLocation) -> str:
    return (
        f"{location} is no longer read. Rename it to {UPTRACE_ENV} for Uptrace "
        f"(DSN https://<token>@api.uptrace.dev?grpc=4317), or use {POSTHOG_ENV} for PostHog "
        "(DSN https://<phc_api_key>@us.i.posthog.com); in pincer.toml, `uptrace_dsn` / `posthog_dsn` "
        "under [telemetry]. Set at most one of them."
    )


def inspect_telemetry(settings: Settings, config_dir: Path | None = None) -> TelemetrySetup:
    """Classify the telemetry configuration, without raising.

    Args:
        settings: The agent settings.
        config_dir: Where to look for the removed setting. Defaults to the cwd.

    Returns:
        `LEGACY` when the removed setting is still configured (checked first);
        `NOT_INSTALLED` when a DSN is set but pincer-telemetry is missing;
        `INVALID` on a configuration error; `ENABLED` with the provider; else `OFF`.
    """
    legacy = legacy_telemetry_dsn(config_dir)
    if legacy is not None:
        return TelemetrySetup(TelemetryState.LEGACY, message=_legacy_message(legacy))
    try:
        from pincer_telemetry import TelemetryConfigError
    except ImportError:
        state = TelemetryState.NOT_INSTALLED if telemetry_configured(settings) else TelemetryState.OFF
        return TelemetrySetup(state)
    try:
        provider = resolve_telemetry_provider(settings)
    except TelemetryConfigError as e:
        return TelemetrySetup(TelemetryState.INVALID, message=str(e))
    if provider is None:
        return TelemetrySetup(TelemetryState.OFF)
    return TelemetrySetup(TelemetryState.ENABLED, provider=provider)
