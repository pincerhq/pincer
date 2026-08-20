from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic.networks import AnyHttpUrl


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class CoreSettings(BaseModel):
    # ── General ───────────────────────────────────────────
    base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://localhost:8080"),
        description="Application domain (e.g. localhost, example.com)",
    )
    # ── Storage ───────────────────────────────────────────
    data_dir: Path = Field(
        default=Path.home() / ".pincer",
        description="Data directory for database, logs, etc.",
    )
    skills_dir: Path = Field(
        default=Path.home() / ".pincer/skills",
        description="User-installed skills directory (SKILL.md subdirectories)",
    )
    skills_max_loaded_per_root: int = Field(
        default=100,
        ge=1,
        description="Max skills loaded per root directory (bundled/user)",
    )
    skills_max_prompt_tokens: int | None = Field(
        default=None,
        description="Soft budget for the Available Skills prompt block; None disables truncation",
    )
    mcp_instructions_max_chars: int = Field(
        default=400,
        ge=0,
        description="Per-server truncation length for MCP server 'instructions' text surfaced in the system prompt",
    )

    # ── Logging ───────────────────────────────────────────
    log_level: LogLevel = LogLevel.INFO

    # ── Cost Controls ─────────────────────────────────────
    daily_budget_usd: float = Field(
        default=5.0,
        ge=0.0,
        description="Daily spend limit in USD (0 = unlimited)",
    )

    # ── Scheduler / Proactive ─────────────────────────────
    briefing_time: str = Field(default="07:00", description="Morning briefing time HH:MM")
    briefing_timezone: str = Field(default="Europe/Berlin", description="Briefing timezone")
    timezone: str = Field(default="Europe/Berlin", description="Default timezone")

    # ── Local Tunnel ──────────────────────────────────────
    ngrok_authtoken: SecretStr = Field(
        default=SecretStr(""),
        description="ngrok auth token — activates built-in tunnel for local dev (PINCER_NGROK_AUTHTOKEN)",
    )
    ngrok_domain: str = Field(
        default="",
        description="ngrok static domain, e.g. my-bot.ngrok-free.app (PINCER_NGROK_DOMAIN)",
    )

    @model_validator(mode="after")
    def check_ngrok_domain(self) -> Self:
        """Default the ngrok domain when only the token is set: the ngrok host
        of the voice webhook URL if there is one, else base_url's host. Never
        localhost (ngrok rejects it)."""
        if self.ngrok_domain or not self.ngrok_authtoken:
            return self
        from urllib.parse import urlsplit

        voice_base = str(getattr(self, "voice_webhook_base_url", "") or "").strip()
        voice_host = urlsplit(voice_base).hostname or "" if voice_base else ""
        if voice_host and ".ngrok" in voice_host:
            self.ngrok_domain = voice_host
            return self
        host = self.base_url.host or ""
        if host and host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            self.ngrok_domain = host  # noqa: F841
        return self
