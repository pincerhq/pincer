from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from pincer.config.dsn import BackendDsn  # noqa: TC001 (pydantic resolves it at runtime)
from pincer.config.prefix import validate_prefix_template


class TaskSettings(BaseModel):
    # ── Background task execution (repid) ─────────────────
    # Both default to the cache settings (`Settings.resolve_task_backend`); set
    # PINCER_TASK_BROKER_URL / PINCER_TASK_PREFIX only to give tasks their own.
    task_broker_url: BackendDsn | None = Field(
        default=None,
        description="Broker for background task execution: memory:// (in-process) or redis://host:6379/0. "
        "Defaults to PINCER_CACHE_BACKEND_URL.",
    )
    task_prefix: str | None = Field(
        default=None,
        description="Template for the prefix of the task streams, consumer groups and pub/sub channels. "
        "Defaults to PINCER_CACHE_PREFIX.",
    )
    task_max_retries: int = Field(
        default=3,
        ge=1,
        description="Max attempts for a background task actor before giving up (repid has no built-in retry)",
    )
    task_poll_interval: int = Field(
        default=60,
        ge=5,
        description="Seconds between checks for due cron schedules (repid has no native scheduler)",
    )

    @field_validator("task_prefix")
    @classmethod
    def _validate_task_prefix(cls, value: str | None) -> str | None:
        return None if value is None else validate_prefix_template(value, "task_prefix")
