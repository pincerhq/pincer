"""Conversation sessions: `sessions`.

`created_at` / `updated_at` are epoch seconds. The model maps the table's
`metadata` column to `session_metadata`, since SQLAlchemy reserves the name.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, json_text_field, upsert
from pincer.models.sessions import MEMORY_FREE_KEY, ChatSession
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


def rekeyed_session_id(session_id: str, old_user_id: str, new_user_id: str) -> str | None:
    """The key a session moves to when its user is renamed, or None if it stays.

    Session keys are `<prefix>:<user_id>`, where the prefix is a channel, a
    Slack or Teams conversation, or `unified`, or a web chat conversation
    nested under one of those: `<prefix>:<user_id>:<conversation uuid>`. The
    id must be a whole segment, exactly the old id; a key that only contains
    it does not move. `voice:` keys belong to a call, not a user, and never do.
    """
    if session_id.startswith("voice:"):
        return None
    suffix = f":{old_user_id}"
    if session_id.endswith(suffix):
        return f"{session_id.removesuffix(suffix)}:{new_user_id}"
    head, _, conversation = session_id.rpartition(":")
    if head.endswith(suffix) and _is_conversation_id(conversation):
        return f"{head.removesuffix(suffix)}:{new_user_id}:{conversation}"
    return None


def _is_conversation_id(segment: str) -> bool:
    """A web chat conversation id: a canonical lowercase UUID (see `api/chat.py`)."""
    try:
        return str(uuid.UUID(segment)) == segment
    except ValueError:
        return False


class SessionRepository(BaseRepository[ChatSession, str]):
    model = ChatSession

    async def newest_for(self, user_id: str, channel: str, *, exclude_prefix: str | None = None) -> ChatSession | None:
        """The most recently updated session for a channel user.

        The fallback lookup for a session stored under the old
        `channel:user_id` key, before cross-channel identities existed.
        Sessions whose id starts with `exclude_prefix` are skipped. The
        prefix is compared exactly, case included, on both dialects: a
        `substr` equality rather than LIKE, which has wildcards and on SQLite
        folds ASCII case.
        """
        stmt = select(ChatSession).where(col(ChatSession.user_id) == user_id, col(ChatSession.channel) == channel)
        if exclude_prefix is not None:
            stmt = stmt.where(func.substr(col(ChatSession.session_id), 1, len(exclude_prefix)) != exclude_prefix)
        stmt = stmt.order_by(col(ChatSession.updated_at).desc()).limit(1)
        return (await self.session.exec(stmt)).first()

    async def exists_for(self, user_id: str, channel: str) -> bool:
        """True when the channel user has any stored session, conversations included.

        A memory-free conversation (a call summary, a knowledge-base question)
        is not counted: it is not the user talking to Pincer, so it must not
        make a new user look known and skip their onboarding.
        """
        memory_free = json_text_field(dialect_of(self.session), col(ChatSession.session_metadata), MEMORY_FREE_KEY)
        stmt = (
            select(ChatSession.session_id)
            .where(
                col(ChatSession.user_id) == user_id,
                col(ChatSession.channel) == channel,
                or_(memory_free.is_(None), memory_free != "true"),
            )
            .limit(1)
        )
        return (await self.session.exec(stmt)).first() is not None

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
        """Re-key the old id's sessions to the new id (`rekeyed_session_id`).

        When a merge finds the target key taken, the more recently updated
        conversation is kept (the target's on a tie) and the other dropped.
        Returns the number of rows changed or removed.
        """
        suffix = f":{old_user_id}"
        keys = select(ChatSession.session_id).where(
            or_(
                col(ChatSession.session_id).endswith(suffix, autoescape=True),
                col(ChatSession.session_id).contains(f"{suffix}:", autoescape=True),
            ),
            ~col(ChatSession.session_id).startswith("voice:", autoescape=True),
        )
        candidates = (await self.session.exec(keys)).all()
        moves = {
            key: new_key
            for key in candidates
            if (new_key := rekeyed_session_id(key, old_user_id, new_user_id)) is not None
        }
        if not moves:
            return 0
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
