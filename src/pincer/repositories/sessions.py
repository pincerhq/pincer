"""Conversation sessions: `sessions`.

`created_at` / `updated_at` are epoch seconds. The model maps the table's
`metadata` column to `session_metadata`, since SQLAlchemy reserves the name.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, update
from sqlalchemy import select as sa_select
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

    async def exists_for(self, user_id: str, channel: str, *, created_before: float | None = None) -> bool:
        """True when the channel user has any stored session, conversations included.

        With `created_before`, only a session created before that time counts.

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
        if created_before is not None:
            stmt = stmt.where(col(ChatSession.created_at) < created_before)
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

        When a merge finds the target key taken, the two conversations are
        joined into the target (`_merged_session`) and the old row removed, so
        no history is lost. Returns the number of rows changed or removed.
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
        taken = select(ChatSession.session_id).where(col(ChatSession.session_id).in_(list(moves.values())))
        existing = set((await self.session.exec(taken)).all())
        collisions = {old: new for old, new in moves.items() if new in existing}
        rows: dict[str, dict[str, Any]] = {}
        if collisions:
            # Only colliding rows' histories are read; a plain move needs none.
            stmt: Select[Any] = sa_select(
                col(ChatSession.session_id),
                col(ChatSession.messages),
                col(ChatSession.session_metadata).label("session_metadata"),
                col(ChatSession.created_at),
                col(ChatSession.updated_at),
            ).where(col(ChatSession.session_id).in_([*collisions, *collisions.values()]))
            connection = await self.session.connection()
            rows = {row.session_id: row._asdict() for row in await connection.execute(stmt)}
        changed = 0
        for old_key, new_key in moves.items():
            if old_key in collisions:
                merged = _merged_session(rows[old_key], rows[new_key])
                target = update(ChatSession).where(col(ChatSession.session_id) == new_key).values(**merged)
                changed += int((await self.session.exec(target)).rowcount)
                changed += await self.delete_where(col(ChatSession.session_id) == old_key)
                continue
            rekey = update(ChatSession).where(col(ChatSession.session_id) == old_key).values(session_id=new_key)
            changed += int((await self.session.exec(rekey)).rowcount)
        return changed

    async def ids(self) -> Sequence[str]:
        return (await self.session.exec(select(ChatSession.session_id))).all()


def _merged_session(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """One row's content from two that collide on a key during a merge.

    Messages carry no timestamps, so the conversations are joined whole: the
    one created first goes first (the target on a tie). A list saved mid-turn
    may end on a tool call with no result; the agent's tool-pair repair drops
    it on the next turn wherever it now sits. Metadata is combined with the
    more recently updated row's keys winning (the target's on a tie), so a
    `memory_free` flag on either side stays: the joined conversation holds
    what was said off the record. The times span both rows.
    """
    first, second = (source, target) if source["created_at"] < target["created_at"] else (target, source)
    older, newer = (target, source) if source["updated_at"] > target["updated_at"] else (source, target)
    return {
        "messages": json.dumps(_loads(first["messages"], []) + _loads(second["messages"], [])),
        "session_metadata": json.dumps(
            {**_loads(older["session_metadata"], {}), **_loads(newer["session_metadata"], {})}
        ),
        "created_at": min(source["created_at"], target["created_at"]),
        "updated_at": max(source["updated_at"], target["updated_at"]),
    }


def _loads(text: str | None, default: Any) -> Any:
    return json.loads(text) if text else default
