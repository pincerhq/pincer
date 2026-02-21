"""
Pincer configuration system.

Loads settings from environment variables and .env files using pydantic-settings.
All secrets use SecretStr to prevent accidental logging.

Environment variable prefix: PINCER_
Example: PINCER_ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    """Main configuration for Pincer agent."""

    model_config = SettingsConfigDict(
        env_prefix="PINCER_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────
    default_provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Anthropic API key")
    openai_api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")

    default_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Default model identifier",
    )
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # ── Channels ─────────────────────────────────────────
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), description="Telegram bot token")
    telegram_allowed_users: list[int] = Field(
        default_factory=list,
        description="Telegram user IDs allowed to use the bot (empty = allow all)",
    )

    # ── Agent ────────────────────────────────────────────
    agent_name: str = Field(default="Pincer", description="Agent display name")
    system_prompt: str = Field(
        default=(
            "You are Pincer, a helpful personal AI assistant. "
            "You are concise, friendly, and proactive. "
            "You have access to tools and use them when they help answer the user's question. "
            "When uncertain, say so honestly. "
            "Always respond in the same language the user writes in.\n\n"
            "IMPORTANT: When you have image or GIF URLs, you MUST use the send_image tool "
            "to display them visually in the chat. NEVER paste image/GIF URLs as plain text. "
            "Call send_image for each image URL so the user sees the actual picture inline."
        ),
        description="System prompt (the agent's personality / soul)",
    )
    max_tool_iterations: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max ReAct loop iterations before forcing a response",
    )
    max_session_messages: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Max messages in session before trimming",
    )

    # ── Cost Controls ────────────────────────────────────
    daily_budget_usd: float = Field(
        default=5.0,
        ge=0.0,
        description="Daily spend limit in USD (0 = unlimited)",
    )

    # ── Storage ──────────────────────────────────────────
    data_dir: Path = Field(
        default=Path.home() / ".pincer",
        description="Data directory for database, logs, etc.",
    )

    # ── Logging ──────────────────────────────────────────
    log_level: LogLevel = LogLevel.INFO

    # ── Search ───────────────────────────────────────────
    tavily_api_key: SecretStr = Field(default=SecretStr(""), description="Tavily API key")

    # ── Shell ────────────────────────────────────────────
    shell_enabled: bool = Field(default=True, description="Enable shell execution tool")
    shell_timeout: int = Field(default=30, ge=5, le=300, description="Shell command timeout secs")
    shell_require_approval: bool = Field(
        default=True,
        description="Require user approval before running shell commands",
    )

    # ── Memory ───────────────────────────────────────────
    memory_enabled: bool = Field(default=True, description="Enable memory system")
    summary_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Cheap model for conversation summarization",
    )
    summary_threshold: int = Field(
        default=20, ge=5, description="Summarize conversation after N messages"
    )

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def parse_allowed_users(cls, v: str | list[int]) -> list[int]:
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        return v

    @model_validator(mode="after")
    def validate_api_keys(self) -> Settings:
        """Ensure at least one LLM provider API key is set."""
        anthropic_set = self.anthropic_api_key.get_secret_value() != ""
        openai_set = self.openai_api_key.get_secret_value() != ""
        if not anthropic_set and not openai_set:
            raise ValueError(
                "At least one LLM API key required. "
                "Set PINCER_ANTHROPIC_API_KEY or PINCER_OPENAI_API_KEY."
            )
        if self.default_provider == LLMProvider.ANTHROPIC and not anthropic_set:
            if openai_set:
                object.__setattr__(self, "default_provider", LLMProvider.OPENAI)
            else:
                raise ValueError("PINCER_ANTHROPIC_API_KEY required for Anthropic provider.")
        if self.default_provider == LLMProvider.OPENAI and not openai_set:
            if anthropic_set:
                object.__setattr__(self, "default_provider", LLMProvider.ANTHROPIC)
            else:
                raise ValueError("PINCER_OPENAI_API_KEY required for OpenAI provider.")
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pincer.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor. Cached after first call."""
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings
