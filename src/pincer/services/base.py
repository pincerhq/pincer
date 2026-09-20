"""What every service shares: one database, no connection of its own."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self

from pincer.db.engine import ensure_schema_current, get_database_url

if TYPE_CHECKING:
    from pathlib import Path


class DatabaseService:
    """A service bound to one database URL.

    It holds no connection — `session_scope()` takes one from the shared
    engine per unit of work — so a single instance serves any event loop.
    """

    #: Keep connections open between units of work. Only for a service with a
    #: single owner writing on its own schedule; see `pincer.db.engine`.
    pooled: bool = False

    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @classmethod
    async def for_path(cls, db_path: Path) -> Self:
        """A service on `db_path`, brought to head first."""
        await asyncio.to_thread(ensure_schema_current, db_path)
        return cls(get_database_url(db_path))
