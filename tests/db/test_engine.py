"""The async engine: URL resolution, per-loop caching and SQLite connection settings."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from pincer.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    _asyncpg_connect_args,
    dispose_engines,
    get_database_url,
    get_engine,
    get_sync_url,
    init_database,
    to_async_url,
)


def test_urls_are_mapped_onto_their_async_driver():
    assert to_async_url("sqlite:////tmp/p.db") == "sqlite+aiosqlite:////tmp/p.db"
    assert to_async_url("postgresql://u:secret@h:5432/db") == "postgresql+asyncpg://u:secret@h:5432/db"
    # An explicit sync driver is replaced, not stacked.
    assert to_async_url("postgresql+psycopg://u@h/db") == "postgresql+asyncpg://u@h/db"


def test_an_unsupported_backend_is_refused():
    with pytest.raises(ValueError, match="mysql"):
        to_async_url("mysql://u@h/db")


def test_the_runtime_url_follows_the_migration_url(monkeypatch, tmp_path):
    monkeypatch.delenv("PINCER_DATABASE_URL", raising=False)
    assert get_database_url(tmp_path / "p.db") == f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"

    monkeypatch.setenv("PINCER_DATABASE_URL", "sqlite:////elsewhere/x.db")
    assert get_database_url(tmp_path / "p.db") == "sqlite+aiosqlite:////elsewhere/x.db"


def test_postgres_is_addressable_now_that_every_domain_uses_the_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u:secret@h/db")
    assert get_database_url(tmp_path / "p.db") == "postgresql+asyncpg://u:secret@h/db"
    # Alembic runs synchronously, so migrations use the sync driver. The
    # password has to survive being re-rendered, or Alembic authenticates as
    # `***` and the process dies at startup on a password error.
    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u:secret@h/db"


def test_an_unsupported_backend_in_the_configured_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_DATABASE_URL", "mysql://u@h/db")
    with pytest.raises(RuntimeError, match="mysql"):
        get_sync_url(tmp_path / "p.db")


def test_an_async_driver_in_the_configured_url_still_gives_alembic_a_sync_one(monkeypatch, tmp_path):
    """Alembic's `engine_from_config` is synchronous: an async driver reaches it
    as `MissingGreenlet` at startup, naming nothing that points back here."""
    monkeypatch.setenv("PINCER_DATABASE_URL", "sqlite+aiosqlite:////tmp/x.db")
    assert get_sync_url(tmp_path / "p.db") == "sqlite+pysqlite:////tmp/x.db"


def test_libpq_keys_reach_asyncpg_through_a_dsn_not_as_keywords():
    """Every managed Postgres hands out `?sslmode=require`; asyncpg.connect()
    has no `sslmode` keyword, so passing it through failed every query."""
    target, connect_args = _asyncpg_connect_args(
        "postgresql+asyncpg://u:p@db.example:6543/d?sslmode=require&application_name=pincer&target_session_attrs=read-write"
    )
    assert dict(target.query) == {"target_session_attrs": "read-write"}  # asyncpg takes this one itself
    assert (target.host, target.port, target.database) == ("db.example", 6543, "d")
    assert connect_args == {"dsn": "postgresql://?sslmode=require&application_name=pincer"}


def test_channel_binding_is_dropped_unless_it_is_required():
    target, connect_args = _asyncpg_connect_args("postgresql+asyncpg://u@h/d?channel_binding=prefer")
    assert dict(target.query) == {}
    assert connect_args == {"dsn": "postgresql://?"}
    with pytest.raises(ValueError, match="channel_binding"):
        _asyncpg_connect_args("postgresql+asyncpg://u@h/d?channel_binding=require")


async def test_a_sslmode_url_opens_an_engine_that_reaches_the_socket():
    """The reviewer's reproduction: before, `connect()` got an unexpected `sslmode` keyword."""
    pytest.importorskip("asyncpg")
    engine = get_engine(to_async_url("postgresql://u:p@127.0.0.1:1/db?sslmode=disable"))
    with pytest.raises(OSError):
        async with engine.connect():
            pass
    await dispose_engines()


def test_the_migration_lock_sits_next_to_the_database_the_url_names(monkeypatch, tmp_path: Path):
    """Two processes whose settings differ but whose override names one file must share a lock."""
    shared = tmp_path / "shared" / "pincer.db"
    monkeypatch.setenv("PINCER_DATABASE_URL", f"sqlite:///{shared}")

    init_database_path = tmp_path / "settings" / "pincer.db"
    asyncio.run(init_database(init_database_path))

    assert (shared.parent / "pincer.db.migrate.lock").exists()
    assert not init_database_path.parent.exists()


async def test_sqlite_connections_are_never_held_between_units_of_work(tmp_path: Path):
    """A pooled SQLite connection keeps its write lock alive between units of
    work, and the other processes sharing the file get "database is locked".

    Telemetry ran on one pooled connection for a while: it failed a driven
    call in four, and measuring showed it bought no audio-loop time at all.
    """
    engine = get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        await dispose_engines()


async def test_sqlite_connections_get_the_shared_pragmas(tmp_path: Path):
    engine = get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == SQLITE_BUSY_TIMEOUT_MS
            assert (await conn.execute(text("PRAGMA synchronous"))).scalar() == 1  # NORMAL
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 0
    finally:
        await dispose_engines()


async def test_one_engine_per_url_on_a_loop(tmp_path: Path):
    a = to_async_url(f"sqlite:///{tmp_path / 'a.db'}")
    b = to_async_url(f"sqlite:///{tmp_path / 'b.db'}")
    try:
        assert get_engine(a) is get_engine(a)
        assert get_engine(a) is not get_engine(b)
    finally:
        await dispose_engines()
    # Disposal forgets them: the next call builds a fresh engine.
    fresh = get_engine(a)
    try:
        async with fresh.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await dispose_engines()


def test_each_event_loop_gets_its_own_engine(tmp_path: Path):
    """Pooled connections are bound to the loop that opened them."""
    url = to_async_url(f"sqlite:///{tmp_path / 'p.db'}")

    async def engine_used_on_this_loop():
        engine = get_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await dispose_engines()
        return engine

    def on_a_fresh_loop():
        # Not `asyncio.run`: it clears the thread's current event loop on exit,
        # which breaks later sync tests that call `asyncio.get_event_loop()`.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(engine_used_on_this_loop())
        finally:
            loop.close()

    assert on_a_fresh_loop() is not on_a_fresh_loop()


def test_an_engine_needs_a_running_loop(tmp_path: Path):
    with pytest.raises(RuntimeError):
        get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))


def test_several_processes_can_migrate_one_fresh_database_at_once(tmp_path: Path):
    """The prod compose file starts two containers on one volume.

    SQLite has no transactional DDL, so without a lock the loser of this race
    is not rolled back: it dies on a `CREATE TABLE` the winner already ran,
    having left `alembic_version` stamped at a revision only half applied.
    """
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from pincer.db.engine import init_database\n"
        "asyncio.run(init_database(Path(sys.argv[1])))\n"
    )
    db_path = tmp_path / "fresh.db"

    started = [
        subprocess.Popen(  # noqa: S603 - fixed argv, test-local script
            [sys.executable, str(worker), str(db_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    # `communicate()` before looking at the exit code: `wait()` on a child
    # whose output fills the pipe buffer never returns.
    finished = [(process, process.communicate()[1]) for process in started]
    assert [stderr for process, stderr in finished if process.returncode != 0] == []

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1


async def test_init_database_migrates_the_configured_database_and_names_it(monkeypatch, tmp_path: Path):
    """The entry points call this and discard the URL; `db_path=None` is the
    branch `pincer run` actually takes, through the settings."""
    monkeypatch.delenv("PINCER_DATABASE_URL", raising=False)
    db_path = tmp_path / "configured.db"

    class _Settings:
        pass

    settings = _Settings()
    settings.db_path = db_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    try:
        url = await init_database()
        assert url == to_async_url(f"sqlite:///{db_path}")
        with sqlite3.connect(db_path) as conn:
            # Not just "a file exists": the schema is actually at head.
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"alembic_version", "memories", "telephony_calls"} <= tables
    finally:
        await dispose_engines()
