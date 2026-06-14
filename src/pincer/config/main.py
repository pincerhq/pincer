from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pincer.config.api import APISettings
from pincer.config.channels import ChannelSettings
from pincer.config.core import CoreSettings
from pincer.config.llm import LLMSettings
from pincer.config.mcp import MCPSettings
from pincer.config.tools import ToolSettings


class Settings(BaseSettings, LLMSettings, ChannelSettings, ToolSettings, APISettings, CoreSettings, MCPSettings):
    """Main configuration for Pincer agent."""

    model_config = SettingsConfigDict(
        env_prefix="PINCER_",
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_api_keys(self) -> Settings:
        """Ensure at least one usable LLM provider is configured.

        Per-leaf validation (which named provider needs which key/base_url) lives
        in `pincer.llm.router.LLMRouter` at build time so it also covers failovers.
        """
        has_anthropic = self.anthropic_api_key.get_secret_value() != ""
        has_openai = self.openai_api_key.get_secret_value() != ""
        has_openai_compatible = self.openai_compatible_base_url != ""
        has_anthropic_compatible = self.anthropic_compatible_base_url != ""
        if not (has_anthropic or has_openai or has_openai_compatible or has_anthropic_compatible):
            raise ValueError(
                "At least one LLM provider required. Set an API key "
                "(PINCER_ANTHROPIC_API_KEY / PINCER_OPENAI_API_KEY) or a compatible "
                "base URL (PINCER_OPENAI_COMPATIBLE_BASE_URL / PINCER_ANTHROPIC_COMPATIBLE_BASE_URL)."
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pincer.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def google_oauth_dir(self) -> Path:
        """Directory for Google OAuth credentials and token.

        Prefers project-relative data/ (when running from repo root) if
        google_credentials.json exists there; otherwise uses data_dir (e.g. ~/.pincer).
        """
        cwd_data = Path.cwd() / "data"
        if (cwd_data / "google_credentials.json").exists():
            return cwd_data
        return self.data_dir

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


class _RelaxedSettings(Settings):
    """Settings subclass that skips API key validation for read-only CLI commands."""

    @model_validator(mode="after")
    def validate_api_keys(self) -> _RelaxedSettings:  # type: ignore[override]
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor. Cached after first call."""
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings


@lru_cache(maxsize=1)
def get_settings_relaxed() -> Settings:
    """Settings accessor that does not require LLM API keys.

    Use for read-only CLI commands (memory stats, schedule list, audit, etc.)
    that only need paths and storage config.
    """
    settings = _RelaxedSettings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings
