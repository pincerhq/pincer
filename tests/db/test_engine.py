"""The async engine: URL resolution, per-loop caching and SQLite connection settings."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from pincer.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    dispose_engines,
    get_database_url,
    get_engine,
    get_sync_url,
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
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u@h/db")
    assert get_database_url(tmp_path / "p.db") == "postgresql+asyncpg://u@h/db"
    # Alembic runs synchronously, so migrations use the sync driver.
    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u@h/db"


def test_an_unsupported_backend_in_the_configured_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_DATABASE_URL", "mysql://u@h/db")
    with pytest.raises(RuntimeError, match="mysql"):
        get_sync_url(tmp_path / "p.db")


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
