from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
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
    skills_dir: str | Path = Field(
        default=Path.home() / ".pincer/skills",
        description="User skills root (SKILL.md subdirectories): a local path or an s3://, az:// or gs:// URI",
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
    skills_cache_dir: Path | None = Field(
        default=None,
        description="Skills cache: cloudpathlib's copies of remote skill files and the index generation stamp; "
        "defaults to <data_dir>/cache/skills",
    )
    skills_index_cache_ttl_seconds: int = Field(
        default=60,
        ge=0,
        le=60,
        description="Max age in seconds of the skill index served by the dashboard API before it rescans "
        "(0 = rescan on every request). Index changes invalidate it immediately.",
    )
    skills_index_watch_interval_seconds: int = Field(
        default=300,
        ge=0,
        description="How often `pincer run` checks for skill index changes announced by other processes "
        "(e.g. `pincer skills rebuild`) and rescans in the background; 0 = no watcher, requests check themselves",
    )
    # ── Remote skill root credentials (unset = the provider SDK's default chain) ──
    skills_s3_endpoint_url: str | None = Field(
        default=None, description="S3-compatible endpoint, e.g. MinIO or Cloudflare R2"
    )
    skills_s3_region: str | None = Field(default=None, description="AWS region for the skills bucket")
    skills_s3_profile: str | None = Field(default=None, description="AWS shared-config profile name")
    skills_s3_access_key_id: SecretStr | None = Field(default=None, description="AWS access key ID")
    skills_s3_secret_access_key: SecretStr | None = Field(default=None, description="AWS secret access key")
    skills_s3_session_token: SecretStr | None = Field(default=None, description="AWS session token")
    skills_azure_connection_string: SecretStr | None = Field(
        default=None, description="Azure Storage connection string (takes precedence over account URL)"
    )
    skills_azure_account_url: str | None = Field(
        default=None, description="Azure Blob account URL, e.g. https://<account>.blob.core.windows.net"
    )
    skills_azure_credential: SecretStr | None = Field(
        default=None, description="Azure account key or SAS token used with skills_azure_account_url"
    )
    skills_gcs_credentials_file: Path | None = Field(
        default=None, description="Path to a Google service-account JSON key file"
    )
    skills_gcs_project: str | None = Field(default=None, description="Google Cloud project ID")
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

    @field_validator("skills_dir", mode="before")
    @classmethod
    def _keep_remote_skills_uri(cls, value: Any) -> Any:
        """Keep cloud URIs as strings: Path coercion would collapse `s3://` into `s3:/`."""
        from pincer.tools.skills.storage import is_remote_uri

        if isinstance(value, str) and is_remote_uri(value):
            return value
        return Path(value).expanduser()

    @model_validator(mode="after")
    def _default_skills_cache_dir(self) -> Self:
        if self.skills_cache_dir is None:
            self.skills_cache_dir = self.data_dir / "cache" / "skills"
        return self

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
