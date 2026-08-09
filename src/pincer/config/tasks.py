from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskSettings(BaseModel):
    # ── Background task execution (repid) ─────────────────
    task_broker: Literal["memory", "redis"] = Field(
        default="memory",
        description="Broker for background task execution: in-process 'memory' (default, zero infra) or 'redis'",
    )
    task_redis_url: str = Field(
        default="",
        description="Redis connection URL, e.g. redis://localhost:6379/0 (required when task_broker=redis)",
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
