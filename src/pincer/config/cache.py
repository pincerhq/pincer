from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from pincer.config.dsn import DEFAULT_BACKEND_URL, BackendDsn

if TYPE_CHECKING:
    from pathlib import Path


class CacheSettings(BaseModel):
    # ── Cache backend (rate limiting, HTTP cache; task broker default) ──
    cache_backend_url: BackendDsn = Field(
        default=DEFAULT_BACKEND_URL,  # type: ignore[assignment]  # validated to MemoryDsn
        validate_default=True,
        description="Backend for API rate limiting and the HTTP response cache, and the default "
        "task broker: memory:// (default, in-process) or redis://host:6379/0",
    )
    cache_default_ttl: int = Field(
        default=300,
        ge=1,
        description="Seconds a cached HTTP response is kept when the response sets no shorter lifetime",
    )


#: Settings removed when the backend URL scheme replaced `task_broker` and the
#: prefix became shared; value = what to set instead. `pincer run` refuses to
#: start while one is set and `pincer doctor` reports it CRITICAL.
LEGACY_CACHE_ENV: dict[str, str] = {
    "PINCER_TASK_BROKER": "PINCER_CACHE_BACKEND_URL (redis://host:6379/0 or memory://; "
    "PINCER_TASK_BROKER_URL to give tasks their own)",
    "PINCER_REDIS_PREFIX": "PINCER_CACHE_PREFIX (PINCER_TASK_PREFIX to give tasks their own)",
}


def legacy_cache_settings(config_dir: Path | None = None) -> list[str]:
    """One "no longer read" message per removed cache/task setting still configured."""
    from pincer.config.legacy_env import find_env_setting

    messages = []
    for name, replacement in LEGACY_CACHE_ENV.items():
        found = find_env_setting(name, config_dir)
        if found is not None:
            messages.append(f"{found} is no longer read. Use {replacement} instead.")
    return messages
