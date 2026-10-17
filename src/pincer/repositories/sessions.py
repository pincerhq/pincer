"""Conversation sessions: `sessions`.

`created_at` / `updated_at` are epoch seconds. The model maps the table's
`metadata` column to `session_metadata`, since SQLAlchemy reserves the name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import update
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

    async def rewrite_session_ids(self, old_user_id: str, new_user_id: str) -> int:
        """Re-key the `unified:<uid>` session from the old id to the new one.

        Only that key names the pincer id: `<channel>:<id>` keys hold the
        channel's own user id and `voice:<call_sid>` keys hold neither, so
        they stay as they are. When a merge finds a session under both ids,
        the most recently updated conversation is kept and the other dropped.
        Returns the number of rows changed or removed.
        """
        old_key, new_key = f"unified:{old_user_id}", f"unified:{new_user_id}"
        # Plain columns, not ORM objects: re-keying a row the session holds
        # would leave its identity map pointing at the old key.
        stmt = select(ChatSession.session_id, ChatSession.updated_at).where(
            col(ChatSession.session_id).in_([old_key, new_key])
        )
        updated = dict((await self.session.exec(stmt)).all())
        if old_key not in updated:
            return 0
        removed = 0
        if new_key in updated:
            if updated[new_key] >= updated[old_key]:
                return await self.delete_where(col(ChatSession.session_id) == old_key)
            removed = await self.delete_where(col(ChatSession.session_id) == new_key)
        rekey = update(ChatSession).where(col(ChatSession.session_id) == old_key).values(session_id=new_key)
        return removed + int((await self.session.exec(rekey)).rowcount)

    async def ids(self) -> Sequence[str]:
        return (await self.session.exec(select(ChatSession.session_id))).all()
