"""The `[telemetry]` section of pincer.toml / pincer.local.toml.

```toml
[telemetry]
uptrace_dsn = "https://<token>@api.uptrace.dev?grpc=4317"
# or (never both): posthog_dsn = "https://<phc_api_key>@eu.i.posthog.com"
```

The keys feed the `telemetry_uptrace_dsn` / `telemetry_posthog_dsn` settings,
below the environment and `.env` in precedence. pincer.local.toml overrides
pincer.toml key by key. Which provider that selects, and whether the pair is
valid, is decided in `pincer.observability.telemetry`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import PydanticBaseSettingsSource

from pincer.config.legacy_env import SettingLocation
from pincer.config.toml_loader import read_toml_raw

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)

#: Read in this order from the working directory; a later file overrides an earlier one.
TELEMETRY_TOML_FILES: tuple[str, ...] = ("pincer.toml", "pincer.local.toml")
TELEMETRY_SECTION = "telemetry"
#: `[telemetry]` key → settings field.
SETTING_BY_TOML_KEY = {"uptrace_dsn": "telemetry_uptrace_dsn", "posthog_dsn": "telemetry_posthog_dsn"}
#: The removed keys: top-level `telemetry_dsn` (documented for 0.1) and `[telemetry] dsn`.
_LEGACY_TOP_LEVEL_KEY = "telemetry_dsn"
_LEGACY_SECTION_KEY = "dsn"


def _read(path: Path) -> dict[str, Any]:
    try:
        return read_toml_raw(path) or {}
    except ValueError as e:  # tomllib.TOMLDecodeError: reported by the loaders that own the file
        logger.warning("Ignoring the [telemetry] section of unreadable %s: %s", path, e)
        return {}


def _section(raw: dict[str, Any]) -> dict[str, Any]:
    section = raw.get(TELEMETRY_SECTION)
    return section if isinstance(section, dict) else {}


def telemetry_toml_settings(config_dir: Path | None = None) -> dict[str, str]:
    """The telemetry settings the TOML files set.

    Args:
        config_dir: Where pincer.toml / pincer.local.toml live. Defaults to the cwd.

    Returns:
        Settings field name → value, for the non-empty string keys of `[telemetry]`.
        Unknown keys are ignored, as Settings ignores unknown variables.
    """
    base_dir = config_dir or Path.cwd()
    values: dict[str, str] = {}
    for name in TELEMETRY_TOML_FILES:
        section = _section(_read(base_dir / name))
        for key, setting in SETTING_BY_TOML_KEY.items():
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                values[setting] = value.strip()
    return values


def legacy_telemetry_toml(config_dir: Path | None = None) -> SettingLocation | None:
    """Where a removed telemetry key is still set in the TOML files, if anywhere.

    Args:
        config_dir: Where pincer.toml / pincer.local.toml live. Defaults to the cwd.

    Returns:
        The first top-level `telemetry_dsn` or `[telemetry] dsn` found, or None.
    """
    base_dir = config_dir or Path.cwd()
    for name in TELEMETRY_TOML_FILES:
        path = base_dir / name
        raw = _read(path)
        if value := raw.get(_LEGACY_TOP_LEVEL_KEY):
            return SettingLocation(_LEGACY_TOP_LEVEL_KEY, str(path), str(value))
        if value := _section(raw).get(_LEGACY_SECTION_KEY):
            return SettingLocation(f"[{TELEMETRY_SECTION}] {_LEGACY_SECTION_KEY}", str(path), str(value))
    return None


class TelemetryTomlSource(PydanticBaseSettingsSource):
    """A Settings source for the `[telemetry]` section (see the module docstring)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Unused: `__call__` returns the whole mapping at once.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(telemetry_toml_settings())
