"""Conversation sessions: `sessions`.

`created_at` / `updated_at` are epoch seconds. The model maps the table's
`metadata` column to `session_metadata`, since SQLAlchemy reserves the name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.sessions import ChatSession
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class SessionRepository(BaseRepository[ChatSession, str]):
    model = ChatSession

    async def newest_for(self, user_id: str, channel: str) -> ChatSession | None:
        """The most recently updated session for a channel user.

        The fallback lookup for a session stored under the old
        `channel:user_id` key, before cross-channel identities existed.
        """
        stmt = (
            select(ChatSession)
            .where(col(ChatSession.user_id) == user_id, col(ChatSession.channel) == channel)
            .order_by(col(ChatSession.updated_at).desc())
            .limit(1)
        )
        return (await self.session.exec(stmt)).first()

    async def save(self, values: dict[str, Any]) -> None:
        """Insert the session, or update the parts that change.

        `created_at` deliberately keeps its stored value: only the messages,
        the metadata and `updated_at` move.
        """
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                ChatSession,
                values,
                index_elements=["session_id"],
                set_=lambda excluded: {
                    "messages": excluded.messages,
                    "metadata": excluded.metadata,
                    "updated_at": excluded.updated_at,
                },
            )
        )

    async def reassign_user(self, old_user_id: str, new_user_id: str) -> int:
        stmt = update(ChatSession).where(col(ChatSession.user_id) == old_user_id).values(user_id=new_user_id)
        return int((await self.session.exec(stmt)).rowcount)

    async def rewrite_session_ids(self, old_fragment: str, new_fragment: str) -> int:
        """Replace a user id embedded in the session key (`unified:<uid>`)."""
        stmt = (
            update(ChatSession)
            .where(col(ChatSession.session_id).contains(old_fragment))
            .values(session_id=func.replace(col(ChatSession.session_id), old_fragment, new_fragment))
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def ids(self) -> Sequence[str]:
        return (await self.session.exec(select(ChatSession.session_id))).all()
