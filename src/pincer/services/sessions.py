"""Session persistence.

`SessionManager` owns the in-memory cache, trimming and onboarding state; this
is only the reading and writing of rows.
"""

from __future__ import annotations

import weakref
from typing import Any, Protocol

from pincer.db.session import session_scope
from pincer.repositories.sessions import SessionRepository
from pincer.services.base import DatabaseService


class SessionCache(Protocol):
    def forget_identity(self, url: str | None, old_user_id: str, new_user_id: str) -> None: ...


# The in-memory session caches of this process (the agent's and the web chat's
# `SessionManager`), told when an identity rename rewrites their rows.
_caches: weakref.WeakSet[SessionCache] = weakref.WeakSet()


def register_cache(cache: SessionCache) -> None:
    _caches.add(cache)


def forget_identity(url: str | None, old_user_id: str, new_user_id: str) -> None:
    """Drop both ids' cached sessions after a rename moved or merged their rows,
    so the next turn reads the rewritten row instead of saving a stale copy over it.

    Only this process's caches: a rename made by another process reaches them
    when that process restarts.
    """
    for cache in list(_caches):
        cache.forget_identity(url, old_user_id, new_user_id)


class SessionService(DatabaseService):
    @property
    def url(self) -> str | None:
        return self._url

    async def load(
        self, session_id: str, *, user_id: str, channel: str, legacy_fallback: bool = True
    ) -> dict[str, Any] | None:
        """The stored session, or the one left under the pre-unified key.

        Before cross-channel identities, a session was keyed `channel:user_id`.
        A user whose session predates that keeps it, rather than starting over.
        An exact key (``legacy_fallback=False``) never inherits another session,
        and the fallback never adopts one nested under this key
        (``<session_id>:<conversation>``): that is a separate conversation.
        """
        async with session_scope(self._url) as session:
            repo = SessionRepository(session)
            row = await repo.get(session_id)
            if row is None and legacy_fallback:
                row = await repo.newest_for(user_id, channel, exclude_prefix=f"{session_id}:")
            return None if row is None else _as_dict(row)

    async def has_any(self, user_id: str, channel: str, *, created_before: float | None = None) -> bool:
        """True when the channel user has any stored session, conversations included
        (memory-free ones aside); with `created_before`, one created before then."""
        async with session_scope(self._url) as session:
            return await SessionRepository(session).exists_for(user_id, channel, created_before=created_before)

    async def save(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        messages: str,
        metadata: str,
        created_at: float,
        updated_at: float,
    ) -> None:
        async with session_scope(self._url) as session:
            await SessionRepository(session).save(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "channel": channel,
                    "messages": messages,
                    "metadata": metadata,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )


def _as_dict(row: Any) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "user_id": row.user_id,
        "channel": row.channel,
        "messages": row.messages,
        "metadata": row.session_metadata,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
