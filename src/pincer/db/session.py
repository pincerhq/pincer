"""Async sessions: the unit of work services run their repositories in.

Repositories never commit. A service opens one `session_scope()` per unit of
work; everything done inside it commits together on exit, or rolls back
together if the block raises.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from pincer.db.engine import get_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def get_sessionmaker(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the shared engine for `url`.

    `expire_on_commit=False`: services return models after the scope commits,
    and an expired attribute would trigger a lazy load outside any session.
    """
    return async_sessionmaker(get_engine(url), class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope(url: str | None = None) -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception."""
    async with get_sessionmaker(url)() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one unit of work per request, on the configured database."""
    async with session_scope() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]
