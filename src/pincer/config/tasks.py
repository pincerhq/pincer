from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class TaskSettings(BaseModel):
    # ── Background task execution (repid) ─────────────────
    task_broker: Literal["memory", "redis"] = Field(
        default="memory",
        description="Broker for background task execution: in-process 'memory' (default, zero infra) or 'redis'",
    )
    task_broker_url: str = Field(
        default="",
        description="Broker connection URL, e.g. redis://localhost:6379/0 (required when task_broker=redis)",
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

    @model_validator(mode="after")
    def _validate_redis_url(self) -> Self:
        if self.task_broker == "redis" and not self.task_broker_url:
            raise ValueError("task_broker_url is required when task_broker='redis'")
        return self
