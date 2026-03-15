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
    GROK = "grok"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    """Main configuration for Pincer agent."""

    model_config = SettingsConfigDict(
        env_prefix="PINCER_",
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────
    default_provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Anthropic API key")
    openai_api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    grok_api_key: SecretStr = Field(default=SecretStr(""), description="xAI Grok API key")

    default_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Default model identifier",
    )
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    temperature: float = Field(default=0.5, ge=0.0, le=2.0)

    # ── Channels ─────────────────────────────────────────
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), description="Telegram bot token")
    discord_bot_token: SecretStr = Field(default=SecretStr(""), description="Discord bot token")
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
            "Call send_image for each image URL so the user sees the actual picture inline.\n\n"
            "When you create a calendar event, your response MUST include the direct link to the event "
            "(from the tool result). If the tool returns an error, tell the user exactly what went wrong."
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
    summary_threshold: int = Field(default=20, ge=5, description="Summarize conversation after N messages")

    # ── WhatsApp (Sprint 3) ──────────────────────────────
    whatsapp_enabled: bool = Field(default=False, description="Enable WhatsApp channel")
    whatsapp_self_chat_only: bool = Field(
        default=True,
        description="When True, only self-chat and group mentions get a reply; DMs from others ignored.",
    )
    whatsapp_dm_allowlist: str = Field(
        default="",
        description="Comma-separated phone numbers allowed to DM; empty = self-chat and group mentions only.",
    )
    whatsapp_group_trigger: str = Field(
        default="pincer",
        description="Trigger word for group chat mentions",
    )

    # ── Cross-Channel Identity (Sprint 3) ────────────────
    identity_map: str = Field(
        default="",
        description="Cross-channel identity mapping, e.g. telegram:12345=whatsapp:491234567890",
    )
    default_user_id: str = Field(
        default="",
        description="Default pincer_user_id for proactive messages",
    )

    # ── Email (Sprint 3) ─────────────────────────────────
    email_imap_host: str = Field(default="", description="IMAP server host")
    email_imap_port: int = Field(default=993, description="IMAP server port")
    email_smtp_host: str = Field(default="", description="SMTP server host")
    email_smtp_port: int = Field(default=587, description="SMTP port (587=STARTTLS, 465=TLS)")
    email_username: str = Field(default="", description="Email account username")
    email_password: SecretStr = Field(default=SecretStr(""), description="Email account password")
    email_from: str = Field(default="", description="Override sender address")

    # ── Proactive Agent (Sprint 3) ───────────────────────
    openweathermap_api_key: SecretStr = Field(default=SecretStr(""), description="OpenWeatherMap API key")
    newsapi_key: SecretStr = Field(default=SecretStr(""), description="NewsAPI key")
    briefing_time: str = Field(default="07:00", description="Morning briefing time HH:MM")
    briefing_timezone: str = Field(default="Europe/Berlin", description="Briefing timezone")
    timezone: str = Field(default="Europe/Berlin", description="Default timezone")

    # ── Webhooks (Sprint 3) ──────────────────────────────
    webhook_port: int = Field(default=8765, description="Webhook listener port")
    webhook_secret: SecretStr = Field(default=SecretStr(""), description="Webhook HMAC secret")

    # ── Dashboard / API (Sprint 5) ───────────────────────
    dashboard_token: SecretStr = Field(default=SecretStr(""), description="Bearer token for API auth")
    dashboard_host: str = Field(default="127.0.0.1", description="API server bind host")
    dashboard_port: int = Field(default=8080, ge=1, le=65535, description="API server port")

    # ── Image Generation (Sprint 8) ──────────────────────
    image_provider: str = Field(
        default="auto",
        description="Image provider: auto | fal | gemini",
    )
    fal_key: SecretStr = Field(default=SecretStr(""), description="fal.ai API key")
    fal_model: str = Field(default="fal-ai/nano-banana-2", description="fal.ai image model")
    gemini_api_key: SecretStr = Field(default=SecretStr(""), description="Google Gemini API key")
    image_model_gemini: str = Field(default="gemini-2.5-flash-image", description="Gemini image generation model")
    image_max_cost_per_request: float = Field(
        default=0.10, ge=0.0, description="Max cost per image generation request in USD"
    )
    image_daily_limit: int = Field(default=50, ge=0, description="Max image generations per day (0 = unlimited)")

    # ── Signal Messenger (Sprint 7.5) ────────────────────
    signal_enabled: bool = Field(default=False, description="Enable Signal channel")
    signal_api_url: str = Field(default="http://signal-api:8080", description="signal-cli-rest-api base URL")
    signal_pair_url: str = Field(
        default="http://127.0.0.1:8081",
        description="URL for browser-based pairing (host-facing); use when signal-api is in Docker",
    )
    signal_phone_number: str = Field(default="", description="Registered Signal phone number (E.164)")
    signal_allowlist: str = Field(
        default="",
        description="Comma-separated phone numbers allowed to DM; empty = allow all",
    )
    signal_group_reply: str = Field(
        default="mention_only",
        description="Group reply mode: mention_only | all | disabled",
    )
    signal_poll_interval: int = Field(default=2, ge=1, description="Poll interval in seconds")
    signal_receive_mode: str = Field(
        default="websocket",
        description="Receive mode: websocket | poll",
    )

    # ── Voice Calling (Sprint 7) ─────────────────────────
    voice_enabled: bool = Field(default=False, description="Enable voice calling channel")
    twilio_account_sid: str = Field(default="", description="Twilio Account SID")
    twilio_auth_token: SecretStr = Field(default=SecretStr(""), description="Twilio Auth Token")
    twilio_phone_number: str = Field(default="", description="Twilio phone number (E.164)")
    voice_engine: str = Field(
        default="conversation_relay",
        description="Voice engine: conversation_relay | media_streams",
    )
    voice_webhook_base_url: str = Field(default="", description="Public URL for Twilio webhooks")
    deepgram_api_key: SecretStr = Field(default=SecretStr(""), description="Deepgram STT API key")
    elevenlabs_api_key: SecretStr = Field(default=SecretStr(""), description="ElevenLabs TTS API key")
    elevenlabs_voice_id: str = Field(default="", description="ElevenLabs voice ID")
    voice_language: str = Field(default="en-US", description="Primary language for STT")
    voice_max_call_duration: int = Field(default=600, ge=30, le=3600, description="Max call seconds")
    voice_max_hold_time: int = Field(default=300, ge=30, le=600, description="Max IVR hold seconds")
    voice_recording_enabled: bool = Field(default=False, description="Enable call recording")
    voice_consent_mode: str = Field(
        default="one_party",
        description="Recording consent: one_party | two_party | none",
    )
    voice_outbound_enabled: bool = Field(default=False, description="Enable outbound calling")
    voice_outbound_max_daily: int = Field(default=10, ge=1, le=100, description="Max outbound calls/day")
    voice_allowed_callers: str = Field(
        default="*",
        description="Comma-separated E.164 numbers allowed to call (or * for all)",
    )
    voice_filler_phrases: str = Field(
        default="",
        description="Custom filler phrases JSON array (empty = built-in)",
    )

    # ── Security (Sprint 5) ──────────────────────────────
    audit_disabled: bool = Field(default=False, description="Disable audit logging")
    rate_messages_per_min: int = Field(default=30, ge=1, description="Per-user message rate limit")
    rate_tools_per_min: int = Field(default=20, ge=1, description="Per-user tool call rate limit")
    max_concurrent_llm: int = Field(default=5, ge=1, description="Max concurrent LLM requests")

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
        grok_set = self.grok_api_key.get_secret_value() != ""
        if not anthropic_set and not openai_set and not grok_set:
            raise ValueError(
                "At least one LLM API key required. "
                "Set PINCER_ANTHROPIC_API_KEY, PINCER_OPENAI_API_KEY, or PINCER_GROK_API_KEY."
            )
        if self.default_provider == LLMProvider.ANTHROPIC and not anthropic_set:
            if openai_set:
                object.__setattr__(self, "default_provider", LLMProvider.OPENAI)
            elif grok_set:
                object.__setattr__(self, "default_provider", LLMProvider.GROK)
            else:
                raise ValueError("PINCER_ANTHROPIC_API_KEY required for Anthropic provider.")
        if self.default_provider == LLMProvider.OPENAI and not openai_set:
            if anthropic_set:
                object.__setattr__(self, "default_provider", LLMProvider.ANTHROPIC)
            elif grok_set:
                object.__setattr__(self, "default_provider", LLMProvider.GROK)
            else:
                raise ValueError("PINCER_OPENAI_API_KEY required for OpenAI provider.")
        if self.default_provider == LLMProvider.GROK and not grok_set:
            if anthropic_set:
                object.__setattr__(self, "default_provider", LLMProvider.ANTHROPIC)
            elif openai_set:
                object.__setattr__(self, "default_provider", LLMProvider.OPENAI)
            else:
                raise ValueError("PINCER_GROK_API_KEY required for Grok provider.")
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor. Cached after first call."""
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings


class _RelaxedSettings(Settings):
    """Settings subclass that skips API key validation for read-only CLI commands."""

    @model_validator(mode="after")
    def validate_api_keys(self) -> _RelaxedSettings:  # type: ignore[override]
        return self


@lru_cache(maxsize=1)
def get_settings_relaxed() -> Settings:
    """Settings accessor that does not require LLM API keys.

    Use for read-only CLI commands (memory stats, schedule list, audit, etc.)
    that only need paths and storage config.
    """
    settings = _RelaxedSettings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings
