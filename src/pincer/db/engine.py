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
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Async driver per backend. Alembic and `get_sync_url` speak the plain
#: backend name; the runtime engine needs the asyncio driver for it.
_ASYNC_DRIVERS = {"sqlite": "aiosqlite", "postgresql": "asyncpg"}

#: The driver Alembic runs on, per backend.
_SYNC_DRIVERS = {"sqlite": "sqlite+pysqlite", "postgresql": "postgresql+psycopg"}

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
# It is process-local, which is why the upgrade itself takes a file lock: see
# `_migration_lock`.
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
        parsed = make_url(override)
        backend = parsed.get_backend_name()
        if backend not in _SYNC_DRIVERS:
            raise RuntimeError(f"PINCER_DATABASE_URL names {backend!r}; supported backends are {sorted(_SYNC_DRIVERS)}")
        # Alembic runs synchronously, so the driver in the override is replaced
        # with the sync one for this backend even though everything else uses
        # the asyncio driver. An async driver here would otherwise reach
        # Alembic's synchronous `engine_from_config` and fail at startup with
        # `MissingGreenlet`, naming nothing that points back at this variable.
        return parsed.set(drivername=_SYNC_DRIVERS[backend]).render_as_string(hide_password=False)
    return f"sqlite:///{db_path}"


def build_config(db_path: Path) -> Config:
    # Built programmatically rather than relying on alembic.ini's on-disk
    # `script_location` discovery, so migrations can run without an ini file
    # present on the filesystem next to wherever the package is installed.
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_sync_url(db_path))
    return cfg


@contextmanager
def _migration_lock(db_path: Path) -> Iterator[None]:
    """Hold an exclusive lock for the duration of one upgrade.

    Two processes upgrading the same SQLite file at once do not merely contend:
    SQLite has no transactional DDL, so the loser is not rolled back. Both read
    `alembic_version`, both run the same `CREATE TABLE`, and one dies with
    "table already exists" — having left the version stamped at a revision
    whose DDL was only half applied, which the next start will not re-run. The
    prod compose file starts `pincer` and `pincer-tasks` against one volume, so
    a first deploy hits exactly this.

    A lock file next to the database serialises them. It is advisory and local
    to one host, which matches SQLite. Postgres needs none: its DDL *is*
    transactional, so a concurrent upgrade either waits on the `alembic_version`
    row or rolls back whole. Nor does a database with no file to share — an
    in-memory one is private to the process that opened it.
    """
    url = make_url(get_sync_url(db_path))
    on_disk = url.get_backend_name() == "sqlite" and (url.database or ":memory:") != ":memory:"
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        on_disk = False
    if not on_disk:
        yield
        return

    lock_path = db_path.parent / f"{db_path.name}.migrate.lock"
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def ensure_schema_current(db_path: Path) -> None:
    """Apply any pending Alembic migrations to `db_path`, bringing it to head.

    Blocking/synchronous — callers on the event loop must wrap this in
    `await asyncio.to_thread(ensure_schema_current, db_path)`.
    """
    resolved = str(Path(db_path).resolve())
    if resolved in _ensured_paths:
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lock(db_path):
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


def get_engine(url: str | None = None) -> AsyncEngine:
    """The shared async engine for `url` (default: the configured database).

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
    engine = per_loop.get(resolved)
    if engine is None:
        engine = per_loop[resolved] = _create_engine(resolved)
    return engine


async def dispose_engines() -> None:
    """Close every engine opened on the running loop. Call at shutdown."""
    per_loop = _engines.pop(asyncio.get_running_loop(), {})
    for engine in per_loop.values():
        await engine.dispose()


def _create_engine(url: str) -> AsyncEngine:
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        # NullPool, always: a pooled SQLite connection keeps its transaction
        # state (and so its write lock) alive between units of work, which
        # starves the other processes and event loops that share one file.
        # Opening per unit of work is what the aiosqlite stores did all along.
        # Telemetry ran on a single pooled connection for a while, on the
        # theory that a thread per write cost audio-loop time; measured, it
        # cost none (p95 0.4-0.9 ms either way) and it failed one driven call
        # in four with "database is locked".
        engine = create_async_engine(url, poolclass=NullPool)
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
    entry points — the API lifespan, and `_build_core`, which both `pincer run`
    and `pincer run tasks` go through — covers all of them. Returns the runtime
    URL.
    """
    if db_path is None:
        from pincer.config import get_settings_relaxed

        db_path = get_settings_relaxed().db_path
    await asyncio.to_thread(ensure_schema_current, db_path)
    return get_database_url(db_path)
