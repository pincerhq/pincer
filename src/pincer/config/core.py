from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class CoreSettings(BaseModel):
    # ── Storage ───────────────────────────────────────────
    data_dir: Path = Field(
        default=Path.home() / ".pincer",
        description="Data directory for database, logs, etc.",
    )
    skills_dir: Path = Field(
        default=Path.home() / ".pincer/skills",
        description="Skills directory (deprecated)",
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
    openweathermap_api_key: SecretStr = Field(default=SecretStr(""), description="OpenWeatherMap API key")
    newsapi_key: SecretStr = Field(default=SecretStr(""), description="NewsAPI key")
    briefing_time: str = Field(default="07:00", description="Morning briefing time HH:MM")
    briefing_timezone: str = Field(default="Europe/Berlin", description="Briefing timezone")
    timezone: str = Field(default="Europe/Berlin", description="Default timezone")
