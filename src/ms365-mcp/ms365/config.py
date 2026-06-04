"""Configuration for the Microsoft 365 MCP server via environment variables."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource, PydanticBaseSettingsSource

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

_DEFAULT_SERVICES = ["email", "calendar", "onedrive", "todo", "contacts", "onenote"]


class _CommaAwareEnvSource(EnvSettingsSource):  # type: ignore[misc]
    """Env source that accepts comma-separated strings for list[str] fields."""

    def decode_complex_value(self, field_name: str, field: FieldInfo, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except (ValueError, json.JSONDecodeError):
            if isinstance(value, str):
                return [s.strip() for s in value.split(",") if s.strip()]
            raise


class MS365Settings(BaseSettings):  # type: ignore[misc]
    model_config = SettingsConfigDict(
        env_prefix="MS365_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8"
    )

    enabled: bool = True
    client_id: str = ""
    tenant_id: str = "consumers"
    auth_method: str = "device_code"
    services: list[str] = list(_DEFAULT_SERVICES)
    token_cache_path: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _CommaAwareEnvSource(settings_cls), dotenv_settings, file_secret_settings)

    @property
    def cache_path(self) -> Path:
        if self.token_cache_path:
            return Path(self.token_cache_path).expanduser()
        return Path.home() / ".pincer" / "ms365_token_cache.json"


@lru_cache(maxsize=1)
def get_settings() -> MS365Settings:
    return MS365Settings()
