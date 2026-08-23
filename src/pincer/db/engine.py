"""URL resolution and migration runner for Pincer's Alembic-managed schema.

Alembic owns schema DDL only — application queries keep using `aiosqlite`
directly (see the module docstrings under `pincer.memory`, `pincer.core`,
etc.). This module's sole job is bringing a database file to the latest
schema revision before any of those modules open their own connection to it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Paths already confirmed at head in this process. Alembic's own
# `alembic_version` table already makes upgrade("head") a cheap no-op once
# current, but this cache also skips the repeated Config/connection setup
# that runs on every one of the ~8 modules' `.initialize()` calls per process.
#
# This guard is process-local only: it does not protect against two separate
# OS processes (e.g. a manual `pincer db upgrade` racing a live `pincer run`,
# or multiple server workers) upgrading the same file concurrently. SQLite's
# busy-timeout means the worst case there is a transient "database is locked"
# error, not corruption — but it is not deduplicated the way in-process calls
# are.
_ensured_paths: set[str] = set()


def get_sync_url(db_path: Path) -> str:
    """Build the synchronous SQLAlchemy URL Alembic runs migrations against.

    `PINCER_DATABASE_URL` is accepted as an override so the migrations
    themselves can be exercised against Postgres (e.g. in CI), but the
    runtime layer — every module under `pincer.memory`/`pincer.core`/etc. —
    still talks to `db_path` directly via `aiosqlite`. Pointing the override
    at Postgres would migrate a database nothing reads while the app keeps
    querying an empty SQLite file, so it's rejected here rather than left to
    fail confusingly downstream.
    """
    override = os.environ.get("PINCER_DATABASE_URL")
    if override:
        if not override.startswith("sqlite"):
            raise RuntimeError(
                "PINCER_DATABASE_URL targets a non-SQLite database, but the runtime "
                "layer is still aiosqlite. Postgres support is migrations-only for now."
            )
        return override
    return f"sqlite:///{db_path}"


def build_config(db_path: Path) -> Config:
    # Built programmatically rather than relying on alembic.ini's on-disk
    # `script_location` discovery, so migrations can run without an ini file
    # present on the filesystem next to wherever the package is installed.
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_sync_url(db_path))
    return cfg


def ensure_schema_current(db_path: Path) -> None:
    """Apply any pending Alembic migrations to `db_path`, bringing it to head.

    Blocking/synchronous — callers on the event loop must wrap this in
    `await asyncio.to_thread(ensure_schema_current, db_path)`.
    """
    resolved = str(Path(db_path).resolve())
    if resolved in _ensured_paths:
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(build_config(db_path), "head")
    _ensured_paths.add(resolved)
    logger.debug("Schema at head for %s", db_path)
