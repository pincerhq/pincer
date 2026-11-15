from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from pincer.config.api import APISettings
from pincer.config.channels import ChannelSettings
from pincer.config.core import CoreSettings
from pincer.config.llm import LLMSettings
from pincer.config.mcp import MCPSettings
from pincer.config.observability import ObservabilitySettings
from pincer.config.tasks import TaskSettings
from pincer.config.telemetry import TelemetryTomlSource
from pincer.config.tools import ToolSettings
from pincer.core.log_once import LogOncePerKey
from pincer.integrations.google.token_source import CREDENTIALS_FILE, TOKEN_FILES

logger = logging.getLogger(__name__)

# Once per directory (or relative value): google_oauth_dir() runs on every token lookup.
_LEGACY_OAUTH_DIR_LOG = LogOncePerKey(logger)


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The [telemetry] section of pincer.toml, below the environment and .env.
        return (init_settings, env_settings, dotenv_settings, TelemetryTomlSource(settings_cls), file_secret_settings)

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

    def google_oauth_candidates(self) -> list[Path]:
        """Directories searched for Google OAuth files: ``PINCER_GOOGLE_OAUTH_DIR`` alone when set,
        else ``data_dir`` then the deprecated ``./data`` (when it is a different directory)."""
        override = self.google_oauth_dir_override  # `~` already expanded by its validator
        if override is not None:
            if not override.is_absolute():
                _LEGACY_OAUTH_DIR_LOG.log(
                    f"relative:{override}",
                    "PINCER_GOOGLE_OAUTH_DIR=%s is relative, so it depends on the working directory (now %s) — "
                    "set an absolute path",
                    override,
                    Path.cwd(),
                )
            return [override]
        cwd_data = Path.cwd() / "data"
        if cwd_data.resolve() == self.data_dir.resolve():
            return [self.data_dir]
        return [self.data_dir, cwd_data]

    def google_oauth_dir(self) -> Path:
        """Directory for the Google OAuth token, independent of the working directory.

        1. ``PINCER_GOOGLE_OAUTH_DIR``, when set (a relative value is logged: it depends on the cwd);
        2. the first of ``data_dir``, ``./data`` that holds ``google_workspace_token.json``;
        3. else the first that holds the legacy calendar-only ``google_token.json`` (so a leftover
           one in ``data_dir`` does not hide a full token in ``./data``);
        4. else the first that holds ``google_credentials.json``;
        5. otherwise ``data_dir`` (where ``pincer google setup`` writes).

        ``google_credentials.json`` may sit in another candidate than the token:
        :meth:`google_credentials_path` looks it up on its own. ``./data`` is
        deprecated: choosing it is logged once.
        """
        candidates = self.google_oauth_candidates()
        for name in (*TOKEN_FILES, CREDENTIALS_FILE):  # full token, legacy token, then the client
            for directory in candidates:
                if (directory / name).exists():
                    return self._note_oauth_dir(directory)
        return candidates[0]

    def google_credentials_path(self, oauth_dir: Path | None = None) -> Path:
        """``google_credentials.json``: in the OAuth dir when it is there, else in the first other
        candidate directory holding it, else (missing) the path in the OAuth dir.

        Looked up apart from the token, so a leftover token in ``./data`` does not hide the
        client file in ``data_dir`` (or the other way round). Pass *oauth_dir* when the caller
        already resolved :meth:`google_oauth_dir`.
        """
        oauth_dir = oauth_dir if oauth_dir is not None else self.google_oauth_dir()
        for directory in dict.fromkeys([oauth_dir, *self.google_oauth_candidates()]):
            path = directory / CREDENTIALS_FILE
            if path.exists():
                self._note_oauth_dir(directory)
                return path
        return oauth_dir / CREDENTIALS_FILE

    def _note_oauth_dir(self, directory: Path) -> Path:
        """Log once when Google OAuth files are taken from the deprecated ``./data``."""
        if self.google_oauth_dir_override is None and directory != self.data_dir:
            _LEGACY_OAUTH_DIR_LOG.log(
                str(directory),
                "Using Google OAuth files in %s, found relative to the working directory — deprecated: "
                "move them to %s or set PINCER_GOOGLE_OAUTH_DIR",
                directory,
                self.data_dir,
            )
        return directory

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
