from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pincer.config.api import APISettings
from pincer.config.channels import ChannelSettings
from pincer.config.core import CoreSettings
from pincer.config.llm import LLMSettings
from pincer.config.mcp import MCPSettings
from pincer.config.observability import ObservabilitySettings
from pincer.config.tasks import TaskSettings
from pincer.config.tools import ToolSettings
from pincer.core.log_once import LogOncePerKey

logger = logging.getLogger(__name__)

#: The files `google_oauth_dir()` looks for (token_source.TOKEN_FILES + CREDENTIALS_FILE).
_GOOGLE_OAUTH_FILES = ("google_workspace_token.json", "google_token.json", "google_credentials.json")
# Once per directory: google_oauth_dir() runs on every token lookup.
_LEGACY_OAUTH_DIR_LOG = LogOncePerKey(logger)


def _has_google_oauth_files(directory: Path) -> bool:
    return any((directory / name).exists() for name in _GOOGLE_OAUTH_FILES)


class Settings(
    BaseSettings,
    LLMSettings,
    ChannelSettings,
    ToolSettings,
    APISettings,
    CoreSettings,
    MCPSettings,
    TaskSettings,
    ObservabilitySettings,
):
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

    @model_validator(mode="after")
    def validate_base_urls(self) -> Settings:
        if not self.voice_webhook_base_url:
            self.voice_webhook_base_url = f"https://{self.base_url.host}"
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pincer.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def google_oauth_dir(self) -> Path:
        """Directory for Google OAuth credentials and token, independent of the working directory.

        1. ``PINCER_GOOGLE_OAUTH_DIR``, when set;
        2. ``data_dir``, when it holds a token or ``google_credentials.json``;
        3. ``./data`` (deprecated), only when it holds one of those files — logged once;
        4. otherwise ``data_dir`` (where ``pincer google setup`` writes).
        """
        if self.google_oauth_dir_override is not None:
            return self.google_oauth_dir_override.expanduser()
        if _has_google_oauth_files(self.data_dir):
            return self.data_dir
        cwd_data = Path.cwd() / "data"
        if cwd_data.resolve() != self.data_dir.resolve() and _has_google_oauth_files(cwd_data):
            _LEGACY_OAUTH_DIR_LOG.log(
                str(cwd_data),
                "Using Google OAuth files in %s, found relative to the working directory — deprecated: "
                "move them to %s or set PINCER_GOOGLE_OAUTH_DIR",
                cwd_data,
                self.data_dir,
            )
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
