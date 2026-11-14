"""Find a removed setting that is still configured, so startup can refuse it.

Settings ignores unknown variables (`extra="ignore"`), so a renamed or removed
variable would otherwise be dropped silently. The callers turn a hit into a
"no longer read" refusal (`pincer run`) or a critical check (`pincer doctor`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SettingLocation:
    """Where a setting was found.

    Attributes:
        key: The key as configured, e.g. `PINCER_TELEMETRY_DSN`.
        source: `the environment`, or the path of the `.env` / TOML file.
        value: The configured value. Kept out of `repr` and `str`: it may be a secret.
    """

    key: str
    source: str
    value: str = field(repr=False)

    def __str__(self) -> str:
        return f"{self.key} in {self.source}"


def find_env_setting(name: str, config_dir: Path | None = None) -> SettingLocation | None:
    """Find a non-empty variable in the environment or a `.env` file Settings reads.

    Args:
        name: The variable name, matched case-insensitively as Settings does.
        config_dir: The directory the `.env` paths are relative to. Defaults to the cwd.

    Returns:
        The first hit (environment first, then the `.env` files in Settings' order),
        or None when the variable is unset or empty everywhere.
    """
    wanted = name.upper()
    for key, value in os.environ.items():
        if key.upper() == wanted and value.strip():
            return SettingLocation(key, "the environment", value)

    from dotenv import dotenv_values

    from pincer.config.main import Settings

    base_dir = config_dir or Path.cwd()
    env_files = Settings.model_config.get("env_file") or ()
    if isinstance(env_files, str | os.PathLike):
        env_files = (env_files,)
    for env_file in env_files:
        path = base_dir / env_file
        if not path.is_file():
            continue
        for key, env_value in dotenv_values(path).items():
            if key.upper() == wanted and env_value and env_value.strip():
                return SettingLocation(key, str(path), env_value)
    return None
