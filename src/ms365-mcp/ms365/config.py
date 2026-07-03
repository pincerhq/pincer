"""Configuration for the Microsoft 365 MCP server via environment variables."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource, PydanticBaseSettingsSource

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)

_DEFAULT_SERVICES = ["email", "calendar", "onedrive", "todo", "contacts", "onenote", "directory"]


class _CommaAwareEnvSource(EnvSettingsSource):  # type: ignore[misc]
    """Env source that accepts comma-separated strings for list[str] fields."""

    def decode_complex_value(self, field_name: str, field: FieldInfo, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except (ValueError, json.JSONDecodeError):
            if isinstance(value, str):
                return [s.strip() for s in value.split(",") if s.strip()]
            raise


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class MS365Settings(BaseSettings):  # type: ignore[misc]
    model_config = SettingsConfigDict(
        env_prefix="MS365_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
    )

    log_level: LogLevel = LogLevel.INFO
    client_id: str = ""
    tenant_id: str = "common"
    auth_method: str = "device_code"
    services: list[str] = list(_DEFAULT_SERVICES)
    token_cache_dir: Path = Path.home() / ".pincer" / "ms365_mcp"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _CommaAwareEnvSource(settings_cls), dotenv_settings, file_secret_settings)

    @field_validator("token_cache_dir", mode="before")
    @classmethod
    def dir_str_to_path(cls, value: str | Path) -> Path:
        try:
            path = Path(value).expanduser()
        except TypeError as e:
            raise ValueError(f"Invalid path: {value!r} - {e}") from e

        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Deliberately not a ValueError: pydantic only folds ValueError/AssertionError
            # into a (catchable) ValidationError. A broken cache dir is a fatal
            # environment problem, not a bad-input validation issue, so this must
            # not be swallowed by a caller's `except Exception` — raising SystemExit
            # here propagates unwrapped and always aborts bootstrap.
            logger.critical("Cannot create/access MS365_TOKEN_CACHE_DIR %s: %s", path, e)
            raise SystemExit(f"Cannot create/access MS365_TOKEN_CACHE_DIR {path}: {e}") from e

        return path


@lru_cache(maxsize=1)
def get_settings() -> MS365Settings:
    return MS365Settings()
