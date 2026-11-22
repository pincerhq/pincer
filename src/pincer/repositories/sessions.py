"""Conversation sessions: `sessions`, and their messages and photos.

`created_at` / `updated_at` are aware UTC datetimes, to the millisecond
(`db.types.UtcDateTime`). The model maps the table's `metadata` column to
`session_metadata`, since SQLAlchemy reserves the name.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, case, func, or_, tuple_, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, json_text_field, upsert
from pincer.db.types import UtcDateTime, Uuid7
from pincer.models.identity import profile_id_of
from pincer.models.sessions import MEMORY_FREE_KEY, ChatSession, SessionImage, SessionMessage
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

logger = logging.getLogger(__name__)


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


def _merged_session(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """The session row's content from two that collide on a key during an identity merge.

    `source` and `target` hold `session_metadata`, `created_at` and
    `updated_at` as stored. Their messages are not here: they are rows of their
    own, moved to the target (`SessionMessageRepository.move`), where the two
    conversations interleave by time. Metadata is combined with the more
    recently updated row's keys winning (the target's on a tie), so a
    `memory_free` flag on either side stays: the joined conversation holds what
    was said off the record. The times span both rows. A side whose stored
    metadata is not a JSON object contributes nothing, rather than failing the
    rename.
    """
    older, newer = (target, source) if source["updated_at"] > target["updated_at"] else (source, target)
    return {
        "session_metadata": json.dumps(
            {**_parsed(older["session_metadata"], dict), **_parsed(newer["session_metadata"], dict)}
        ),
        "created_at": min(source["created_at"], target["created_at"]),
        "updated_at": max(source["updated_at"], target["updated_at"]),
    }


def _parsed[T: (list[Any], dict[str, Any])](text: str | None, kind: type[T]) -> T:
    """`text` parsed as `kind`, or an empty one when it is missing, corrupt or another type."""
    try:
        value = json.loads(text) if text else None
    except ValueError:
        value = None
    if isinstance(value, kind):
        return value
    if text:
        logger.warning("Dropping a merged session's stored %s that is not one: %.80r", kind.__name__, text)
    return kind()


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

    async def exists_for(self, user_id: str, channel: str, *, created_before: datetime | None = None) -> bool:
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

    async def save(self, values: dict[str, Any]) -> str:
        """Insert the session, or update the parts that change. Returns its `id`.

        `created_at` deliberately keeps its stored value: only the metadata
        and `updated_at` move. The messages are rows of their own
        (`SessionMessageRepository`).
        """
        stmt = upsert(
            dialect_of(self.session),
            ChatSession,
            values,
            index_elements=["session_id"],
            set_=lambda excluded: {
                "metadata": excluded.metadata,
                "updated_at": excluded.updated_at,
            },
        ).returning(col(ChatSession.id))
        return str((await self.session.exec(stmt)).scalar_one())

    async def move_user(self, old_user_id: str, new_user_id: str) -> int:
        """Move the old id's sessions to the new id: re-key them, then reassign `user_id`.

        Must run inside the rename's transaction; it does not commit, so a
        failure in either step rolls both back with the rest of the rename.

        The order matters. Canonical ids can contain colons (`teams:{aad_id}`),
        so key text alone cannot tell whose row a key is: renaming `abc` would
        also match another user's `web:teams:abc`. Re-keying only rows whose
        `user_id` is still the old id, before it is reassigned, picks out
        exactly the renamed identity's rows. Filtering on the new id afterwards
        would also catch the target's own rows in a merge (`web:teams:abc`
        when merging `abc` into `teams:abc`).

        Returns the number of rows changed or removed.
        """
        changed = await self._rekey_sessions(old_user_id, new_user_id)
        reassign = (
            update(ChatSession)
            .where(col(ChatSession.user_id) == old_user_id)
            .values(user_id=new_user_id, profile_id=profile_id_of(new_user_id))
        )
        return changed + int((await self.session.exec(reassign)).rowcount)

    async def _rekey_sessions(self, old_user_id: str, new_user_id: str) -> int:
        """Re-key the old id's own sessions to the new id (`rekeyed_session_id`).

        When a merge finds the target key taken, the two conversations are
        joined into the target (`_merged_session`) and the old row removed, so
        neither conversation is dropped. Returns the number of rows changed or removed.
        """
        suffix = f":{old_user_id}"
        keys = select(ChatSession.session_id).where(
            col(ChatSession.user_id) == old_user_id,
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
        # Locked until the rename commits, so a turn saved to a target meanwhile
        # waits rather than being overwritten by the merge. SQLite has no row
        # locks; the rename's earlier writes already hold its database write lock.
        times = (
            select(ChatSession.session_id, ChatSession.created_at, ChatSession.updated_at)
            .where(col(ChatSession.session_id).in_([*moves, *moves.values()]))
            .with_for_update()
        )
        rows: dict[str, dict[str, Any]] = {
            key: {"created_at": created_at, "updated_at": updated_at}
            for key, created_at, updated_at in (await self.session.exec(times)).all()
        }
        collisions = {old: new for old, new in moves.items() if new in rows}
        if collisions:
            # Only colliding rows' metadata is read; a plain move needs none.
            content = select(ChatSession.session_id, ChatSession.session_metadata).where(
                col(ChatSession.session_id).in_([*collisions, *collisions.values()])
            )
            for key, metadata in (await self.session.exec(content)).all():
                rows[key] |= {"session_metadata": metadata}
        images = SessionImageRepository(self.session)
        messages = SessionMessageRepository(self.session)
        changed = 0
        for old_key, new_key in moves.items():
            if old_key in collisions:
                merged = _merged_session(rows[old_key], rows[new_key])
                target = update(ChatSession).where(col(ChatSession.session_id) == new_key).values(**merged)
                changed += int((await self.session.exec(target)).rowcount)
                # The joined messages refer to both sides' photos. Both move before the old row goes: on
                # Postgres deleting it cascades to its messages and photos
                await messages.move(old_key, new_key)
                await images.merge(old_key, new_key)
                changed += await self.delete_where(col(ChatSession.session_id) == old_key)
                continue
            # The photos point at the row's `id`, which a new key does not change.
            rekey = update(ChatSession).where(col(ChatSession.session_id) == old_key).values(session_id=new_key)
            changed += int((await self.session.exec(rekey)).rowcount)
        return changed

    async def ids(self) -> Sequence[str]:
        return (await self.session.exec(select(ChatSession.session_id))).all()


def _session_ref(session_id: str) -> Any:
    """The `id` of the session with this key, as a scalar subquery."""
    return select(ChatSession.id).where(col(ChatSession.session_id) == session_id).scalar_subquery()


class SessionImageRepository(BaseRepository[SessionImage, str]):
    model = SessionImage

    async def for_session(self, session_ref: str) -> Sequence[SessionImage]:
        """The photos of the session whose `id` is `session_ref`, oldest first."""
        return await self.list(col(SessionImage.session_ref) == session_ref, order_by=[col(SessionImage.created_at)])

    async def sync(self, session_ref: str, keep: Collection[str], add: Sequence[SessionImage]) -> None:
        """Make the photos of the session whose `id` is `session_ref` exactly `keep`: store the ones
        in `add` that are not there yet (never overwriting a stored one), then delete every other
        row of the session. The session row has to exist already."""
        if add:
            await self.session.exec(
                upsert(
                    dialect_of(self.session),
                    SessionImage,
                    [
                        {
                            "session_ref": session_ref,
                            "sha256": image.sha256,
                            "media_type": image.media_type,
                            "data": image.data,
                            "created_at": image.created_at,
                        }
                        for image in add
                    ],
                    index_elements=["session_ref", "sha256"],
                )
            )
        stale = [col(SessionImage.sha256).notin_(keep)] if keep else []
        await self.delete_where(col(SessionImage.session_ref) == session_ref, *stale)

    async def merge(self, old_session_id: str, new_session_id: str) -> int:
        """Move the photos of `old_session_id` that `new_session_id` does not hold to it, for two sessions
        joined into one; a photo both hold (same hash, so same bytes) is stored once. Whatever is left under
        the old session is deleted, so the old session row can go. Returns the photos moved."""
        target = _session_ref(new_session_id)
        held = select(SessionImage.sha256).where(col(SessionImage.session_ref) == target)
        move = update(SessionImage).where(
            col(SessionImage.session_ref) == _session_ref(old_session_id), col(SessionImage.sha256).notin_(held)
        )
        moved = int((await self.session.exec(move.values(session_ref=target))).rowcount)
        await self.delete_where(col(SessionImage.session_ref) == _session_ref(old_session_id))
        return moved


#: The columns of a message an API shows people: never `context` (Pincer's own per-turn context) or `raw`
#: (the provider's output, replayed to it), which are most of a history's bytes.
DISPLAY_COLUMNS = ("id", "role", "content", "tool_calls", "images", "created_at", "updated_at")

#: Rows per INSERT: a first save of a long history (one taken over from an older key) stays under
#: SQLite's limit on bound parameters.
_INSERT_BATCH = 500

#: The columns a save writes to a message row; `channel_message_id` and `updated_at` belong to `edit`.
_SAVED_COLUMNS = ("role", "content", "tool_call_id", "is_error", "tool_calls", "images", "context", "raw", "wire")


class SessionMessageRepository(BaseRepository[SessionMessage, str]):
    """The messages of sessions, in the order (`created_at`, `id`)."""

    model = SessionMessage

    async def for_session(self, session_ref: str) -> Sequence[SessionMessage]:
        """Every message of the session whose `id` is `session_ref`, in order."""
        return await self.list(
            col(SessionMessage.session_id) == session_ref,
            order_by=[col(SessionMessage.created_at), col(SessionMessage.id)],
        )

    async def page(
        self, session_ref: str, *, limit: int, before: tuple[datetime, str] | None = None
    ) -> list[dict[str, Any]]:
        """The latest `limit` messages of the session before the (`created_at`, `id`) cursor `before`
        (the newest, without one), oldest first, as `DISPLAY_COLUMNS` only."""
        columns = [getattr(SessionMessage, name) for name in DISPLAY_COLUMNS]
        stmt = select(*columns).where(col(SessionMessage.session_id) == session_ref)
        if before is not None:
            cursor = tuple_(
                bindparam("before_at", before[0], type_=UtcDateTime()), bindparam("before_id", before[1], type_=Uuid7())
            )
            stmt = stmt.where(tuple_(col(SessionMessage.created_at), col(SessionMessage.id)) < cursor)
        stmt = stmt.order_by(col(SessionMessage.created_at).desc(), col(SessionMessage.id).desc()).limit(limit)
        rows = (await self.session.exec(stmt)).all()
        return [dict(zip(DISPLAY_COLUMNS, row, strict=True)) for row in reversed(rows)]

    async def write_changes(
        self,
        session_ref: str,
        *,
        insert_rows: Sequence[dict[str, Any]] = (),
        update_rows: Sequence[dict[str, Any]] = (),
        delete_ids: Collection[str] = (),
    ) -> None:
        """Bring the session's rows up to date: delete `delete_ids`, rewrite the written columns of
        `update_rows` (by `id`), and insert `insert_rows`. Only rows of this session are touched, so a
        stale id can never reach another session's history."""
        if delete_ids:
            await self.delete_where(
                col(SessionMessage.session_id) == session_ref, col(SessionMessage.id).in_(list(delete_ids))
            )
        # Core statements on the table, one executemany each: not the ORM's bulk modes
        table = SessionMessage.__table__  # type: ignore[attr-defined]
        conn = await self.session.connection()
        if update_rows:
            stmt = (
                table.update()
                .where(table.c.session_id == session_ref, table.c.id == bindparam("row_id"))
                .values(
                    {name: bindparam(f"new_{name}") for name in _SAVED_COLUMNS}
                    # A row that stops being a user message (a summary taking the place of the first message it
                    # summarizes) drops what only a user message has
                    | {
                        name: case((bindparam("new_role") == "user", table.c[name]), else_=None)
                        for name in ("channel_message_id", "updated_at")
                    }
                )
            )
            params = [
                {"row_id": row["id"]} | {f"new_{name}": row[name] for name in _SAVED_COLUMNS} for row in update_rows
            ]
            await conn.execute(stmt, params)
        # Upserted by id: a save retried after a commit whose answer was lost (its rows are in) rewrites
        # them instead of failing or adding them twice. Only a row of this session is ever rewritten.
        for start in range(0, len(insert_rows), _INSERT_BATCH):
            batch = [row | {"session_id": session_ref} for row in insert_rows[start : start + _INSERT_BATCH]]
            await conn.execute(
                upsert(
                    dialect_of(self.session),
                    table,
                    batch,
                    index_elements=["id"],
                    set_=lambda excluded: {name: excluded[name] for name in (*_SAVED_COLUMNS, "created_at")},
                    where=table.c.session_id == session_ref,
                )
            )

    async def move(self, old_session_id: str, new_session_id: str) -> int:
        """Move every message of `old_session_id` to `new_session_id`, two sessions joined into one: the
        conversations interleave by time. A messenger id both hold names one message, kept on the
        target's copy. Returns the messages moved.

        Lossy for a tool call whose results came later than a message of the other conversation (both
        live at once, within the seconds a tool ran): the agent's tool-pair repair drops that pair on the
        next turn, rewriting the cached request prefix once. Migration 0035 dates each migrated tool group
        at one millisecond, so the histories it moved do not split this way."""
        source, target = _session_ref(old_session_id), _session_ref(new_session_id)
        held = select(SessionMessage.channel_message_id).where(
            col(SessionMessage.session_id) == target, col(SessionMessage.channel_message_id).is_not(None)
        )
        await self.session.exec(
            update(SessionMessage)
            .where(col(SessionMessage.session_id) == source, col(SessionMessage.channel_message_id).in_(held))
            .values(channel_message_id=None)
        )
        moved = await self.session.exec(
            update(SessionMessage).where(col(SessionMessage.session_id) == source).values(session_id=target)
        )
        return int(moved.rowcount)

    async def edit(self, session_ref: str, channel_message_id: str, content: str, *, at: datetime) -> bool:
        """Rewrite the text of the user message the messenger calls `channel_message_id`, edited `at`
        (`content` as a TEXT column takes it: `SessionService.edit` prepares it). False when the session
        has no such message."""
        stmt = (
            update(SessionMessage)
            .where(
                col(SessionMessage.session_id) == session_ref,
                col(SessionMessage.channel_message_id) == channel_message_id,
                col(SessionMessage.role) == "user",
            )
            .values(content=content, updated_at=at)
        )
        return int((await self.session.exec(stmt)).rowcount) > 0
