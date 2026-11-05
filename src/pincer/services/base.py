"""What every service shares: one database, no connection of its own."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self

from pincer.db.engine import ensure_schema_current, get_database_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlmodel.ext.asyncio.session import AsyncSession


class DatabaseService:
    """A service bound to one database URL.

    It holds no connection — `session_scope()` takes one from the shared
    engine per unit of work — so a single instance serves any event loop.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @classmethod
    async def for_path(cls, db_path: Path) -> Self:
        """A service on `db_path`, brought to head first."""
        await asyncio.to_thread(ensure_schema_current, db_path)
        return cls(get_database_url(db_path))


def query_sync[T](db_path: Path, query: Callable[[AsyncSession], Awaitable[T]]) -> T | None:
    """Run one read on the database, for the synchronous health checks
    (`pincer doctor`, the pilot preflight). None when there is no database yet.

    Those checks also run inside a running loop (`GET /api/doctor`), where
    `asyncio.run` refuses to start, so the query gets a loop of its own on a
    worker thread there.
    """
    import concurrent.futures

    from pincer.db.engine import _database_file, dispose_engines, get_sync_url
    from pincer.db.session import session_scope

    # Opening a SQLite file that isn't there creates it; a fresh install has
    # nothing to read, so don't leave an empty database behind.
    database = _database_file(get_sync_url(db_path))
    if database is not None and not database.exists():
        return None

    async def run() -> T:
        try:
            async with session_scope(get_database_url(db_path)) as session:
                return await query(session)
        finally:
            await dispose_engines()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, run()).result()
