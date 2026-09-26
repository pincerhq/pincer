"""Session persistence.

`SessionManager` owns the in-memory cache, trimming and onboarding state; this
is only the reading and writing of rows.
"""

from __future__ import annotations

from typing import Any

from pincer.db.session import session_scope
from pincer.repositories.sessions import SessionRepository
from pincer.services.base import DatabaseService


class SessionService(DatabaseService):
    async def load(self, session_id: str, *, user_id: str, channel: str) -> dict[str, Any] | None:
        """The stored session, or the one left under the pre-unified key.

        Before cross-channel identities, a session was keyed `channel:user_id`.
        A user whose session predates that keeps it, rather than starting over.
        """
        async with session_scope(self._url) as session:
            repo = SessionRepository(session)
            row = await repo.get(session_id)
            if row is None:
                row = await repo.newest_for(user_id, channel)
            return None if row is None else _as_dict(row)

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
