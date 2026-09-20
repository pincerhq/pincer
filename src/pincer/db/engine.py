"""URL resolution, async engines and the migration runner for Pincer's database.

Alembic owns schema DDL. Application queries all run on the async engines built
here (`get_engine`), through the repositories in `pincer.repositories`; nothing
in the runtime opens its own driver connection any more, so the configured URL
may name SQLite or Postgres. `init_database` is the one call a process makes at
startup: it brings the database to the latest revision before anything queries
it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

#: Async driver per backend. Alembic and `get_sync_url` speak the plain
#: backend name; the runtime engine needs the asyncio driver for it.
_ASYNC_DRIVERS = {"sqlite": "aiosqlite", "postgresql": "asyncpg"}

#: How long a SQLite writer waits on another connection's lock before failing.
#: Several processes share one file (the app, the tasks worker, `pincer mcp
#: serve`, one-off CLI commands), so a short wait is normal, not an error.
SQLITE_BUSY_TIMEOUT_MS = 5000

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
    """The synchronous SQLAlchemy URL Alembic runs migrations against.

    `PINCER_DATABASE_URL` overrides it, and may now name Postgres: the runtime
    reads and writes through `pincer.repositories`, which is dialect-agnostic
    (`pincer.db.dialect` covers what differs). Postgres needs the `postgres`
    extra for its drivers — asyncpg at runtime, psycopg for the migrations.
    """
    override = os.environ.get("PINCER_DATABASE_URL")
    if override:
        backend = make_url(override).get_backend_name()
        if backend not in _ASYNC_DRIVERS:
            raise RuntimeError(
                f"PINCER_DATABASE_URL names {backend!r}; supported backends are {sorted(_ASYNC_DRIVERS)}"
            )
        if backend == "postgresql":
            # Alembic runs synchronously, so it needs the sync driver even
            # though everything else uses asyncpg.
            return make_url(override).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
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


# ── async engines ────────────────────────────────────────────────────


def to_async_url(url: str) -> str:
    """The same database, addressed through its asyncio driver."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    driver = _ASYNC_DRIVERS.get(backend)
    if driver is None:
        raise ValueError(f"Unsupported database backend {backend!r}; expected one of {sorted(_ASYNC_DRIVERS)}")
    return parsed.set(drivername=f"{backend}+{driver}").render_as_string(hide_password=False)


def get_database_url(db_path: Path) -> str:
    """The async runtime URL for `db_path`, honouring `PINCER_DATABASE_URL`.

    Derived from `get_sync_url`, so the runtime can never point at a different
    database than the one migrations were applied to.
    """
    return to_async_url(get_sync_url(db_path))


def default_database_url() -> str:
    """The configured database, for callers that are not handed a URL."""
    from pincer.config import get_settings_relaxed

    return get_database_url(get_settings_relaxed().db_path)


# One engine per (event loop, URL). An engine's pooled connections are bound to
# the loop that opened them — aiosqlite runs each on a worker thread that calls
# back into that loop, and asyncpg sockets belong to it — so reusing an engine
# from another loop (`asyncio.run` in the CLI, per-test loops) fails. Keyed
# weakly: a loop that is gone takes its engines with it.
_engines: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncEngine]] = weakref.WeakKeyDictionary()
_engines_pid = os.getpid()


def get_engine(url: str | None = None, *, pooled: bool = False) -> AsyncEngine:
    """The shared async engine for `url` (default: the configured database).

    `pooled` keeps SQLite connections open between units of work instead of
    opening one per transaction. Only for a single owner writing on its own
    schedule — telemetry's sink — where the thread per connection costs more
    than the pool risks. Everything else shares the file with other processes
    and must not hold connections; see `_create_engine`.

    Must be called from a running event loop; see the note on `_engines`.
    """
    global _engines_pid
    if os.getpid() != _engines_pid:
        # A forked child must not reuse the parent's pooled connections.
        _engines.clear()
        _engines_pid = os.getpid()
    resolved = url or default_database_url()
    loop = asyncio.get_running_loop()
    per_loop = _engines.get(loop)
    if per_loop is None:
        per_loop = _engines[loop] = {}
    key = f"{resolved}|pooled" if pooled else resolved
    engine = per_loop.get(key)
    if engine is None:
        engine = per_loop[key] = _create_engine(resolved, pooled=pooled)
    return engine


async def dispose_engines() -> None:
    """Close every engine opened on the running loop. Call at shutdown."""
    per_loop = _engines.pop(asyncio.get_running_loop(), {})
    for engine in per_loop.values():
        await engine.dispose()


def _create_engine(url: str, *, pooled: bool = False) -> AsyncEngine:
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        # NullPool by default: a pooled SQLite connection keeps its transaction
        # state (and so its write lock) alive between units of work, which
        # deadlocks the several processes and event loops that share one file.
        # Opening per unit of work is what the aiosqlite stores did all along.
        # A `pooled` engine keeps ONE connection instead, for an owner that
        # writes on its own schedule and would otherwise pay a thread per write.
        engine = (
            create_async_engine(url, pool_size=1, max_overflow=0, pool_recycle=300)
            if pooled
            else create_async_engine(url, poolclass=NullPool)
        )
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
        return engine
    # A pooled Postgres connection can be dropped server-side while idle.
    return create_async_engine(url, pool_pre_ping=True)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Per-connection SQLite settings, matching what the aiosqlite stores set.

    `foreign_keys` stays OFF deliberately: the declared foreign keys have never
    been enforced, and turning enforcement on could start rejecting writes that
    touch rows already orphaned in existing databases.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


async def init_database(db_path: Path | None = None) -> str:
    """Bring the configured database to head, once, at startup.

    Every store used to do this in its own `initialize()`; one call at the
    entry points (`pincer run`, the API lifespan, `pincer mcp serve`, the
    tasks worker) covers all of them. Returns the runtime URL.
    """
    if db_path is None:
        from pincer.config import get_settings_relaxed

        db_path = get_settings_relaxed().db_path
    await asyncio.to_thread(ensure_schema_current, db_path)
    return get_database_url(db_path)
