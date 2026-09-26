"""Where Pincer's database lives — resolved on its own, apart from `Settings`.

`pincer.db.engine.get_sync_url` builds this on every call instead of reading
the cached `get_settings_relaxed()`: the URL must be right from the very first
call (`init_database` at startup), follow a changed environment, and not fail
because some unrelated `PINCER_*` field is invalid or `data_dir` is read-only.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """`PINCER_DATABASE_URL`, from the environment or `.env` (environment wins)."""

    model_config = SettingsConfigDict(
        env_prefix="PINCER_",
        env_file=("../.env", ".env"),  # same files, same order, as Settings
        env_file_encoding="utf-8",
        # Unlike Settings: an explicitly empty `PINCER_DATABASE_URL=` in the
        # environment is a choice ("use the SQLite file"), so it must beat a
        # URL in `.env` rather than fall through to it.
        env_ignore_empty=False,
        case_sensitive=False,
        extra="ignore",
    )

    database_url: SecretStr | None = Field(
        default=None,
        description="SQLAlchemy URL for Pincer's database (sqlite or postgresql); "
        "unset or empty means the SQLite file under data_dir",
    )
