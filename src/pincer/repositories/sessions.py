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
        """Re-key the old id's sessions to the new id.

        Session keys are `<prefix>:<user_id>`, where the prefix is a channel,
        a Slack or Teams conversation, or `unified`. A key moves when its last
        segment is exactly the old id; a key that only contains it does not.
        `voice:<call_sid>` keys belong to a call, not a user, and never move.
        When a merge finds the target key taken, the more recently updated
        conversation is kept (the target's on a tie) and the other dropped.
        Returns the number of rows changed or removed.
        """
        suffix = f":{old_user_id}"
        keys = select(ChatSession.session_id).where(
            col(ChatSession.session_id).endswith(suffix, autoescape=True),
            ~col(ChatSession.session_id).startswith("voice:", autoescape=True),
        )
        old_keys = (await self.session.exec(keys)).all()
        if not old_keys:
            return 0
        moves = {key: f"{key.removesuffix(suffix)}:{new_user_id}" for key in old_keys}
        # Plain columns, not ORM objects: re-keying a row the session holds
        # would leave its identity map pointing at the old key.
        stmt = select(ChatSession.session_id, ChatSession.updated_at).where(
            col(ChatSession.session_id).in_([*moves, *moves.values()])
        )
        updated = dict((await self.session.exec(stmt)).all())
        changed = 0
        for old_key, new_key in moves.items():
            if new_key in updated:
                if updated[new_key] >= updated[old_key]:
                    changed += await self.delete_where(col(ChatSession.session_id) == old_key)
                    continue
                changed += await self.delete_where(col(ChatSession.session_id) == new_key)
            rekey = update(ChatSession).where(col(ChatSession.session_id) == old_key).values(session_id=new_key)
            changed += int((await self.session.exec(rekey)).rowcount)
        return changed

    async def ids(self) -> Sequence[str]:
        return (await self.session.exec(select(ChatSession.session_id))).all()
