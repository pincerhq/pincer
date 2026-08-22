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

    Honors `PINCER_DATABASE_URL` as a forward-compatible override (e.g. a
    future `postgresql+psycopg://...`) so migrations can target Postgres
    without any code changes here once that driver is added as a dependency.
    """
    override = os.environ.get("PINCER_DATABASE_URL")
    if override:
        return override
    return f"sqlite:///{db_path}"


def build_config(db_path: Path) -> Config:
    # Built programmatically rather than via alembic.ini's relative
    # `script_location` default, which breaks once the package is installed
    # and run from an arbitrary working directory.
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
