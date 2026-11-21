"""Session persistence.

`SessionManager` owns the in-memory cache, trimming and onboarding state; this
is only the reading and writing of rows.

A message crosses this boundary as a dict in `LLMMessage.to_dict`'s format,
plus its row's `id` and `created_at`; the mapping to and from the columns of
`pincer_session_messages` is here (`_message_row`, `_message_dict`).
"""

from __future__ import annotations

import logging
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pincer.db.session import session_scope
from pincer.models.sessions import SessionImage, SessionMessage
from pincer.repositories.sessions import SessionImageRepository, SessionMessageRepository, SessionRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredImage:
    """One photo of a session as the image store holds it (never in a repr: it is megabytes)."""

    sha256: str
    media_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ImageSet:
    """What a session's photos should be once it is saved: exactly the `keep` hashes, with the
    bytes of the ones not stored yet in `add`."""

    keep: frozenset[str]
    add: tuple[StoredImage, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageChanges:
    """What a save does to a session's messages: rows to insert and rows to rewrite (message dicts with
    their `id` and `created_at`), and the ids of the rows to delete."""

    insert: Sequence[dict[str, Any]] = ()
    update: Sequence[dict[str, Any]] = ()
    delete: Sequence[str] = ()

    def __bool__(self) -> bool:
        return bool(self.insert or self.update or self.delete)


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
            row = await repo.get_by(session_id=session_id)
            if row is None and legacy_fallback:
                row = await repo.newest_for(user_id, channel, exclude_prefix=f"{session_id}:")
            if row is None:
                return None
            # By the row's own id: a session left under the pre-unified key keeps its messages and photos there
            messages = await SessionMessageRepository(session).for_session(row.id)
            photos = await SessionImageRepository(session).for_session(row.id)
            return _as_dict(row) | {
                "messages": [_message_dict(message) for message in messages],
                "images": [StoredImage(i.sha256, i.media_type, i.data) for i in photos],
            }

    async def history(
        self, session_id: str, *, limit: int, before: tuple[datetime, str] | None = None
    ) -> list[dict[str, Any]]:
        """A page of the session's messages as people see them (no `context`, no `raw`): the latest
        `limit` before the (`created_at`, `id`) cursor `before`, oldest first. Empty for no such session."""
        async with session_scope(self._url) as session:
            row = await SessionRepository(session).get_by(session_id=session_id)
            if row is None:
                return []
            return await SessionMessageRepository(session).page(row.id, limit=limit, before=before)

    async def has_any(self, user_id: str, channel: str, *, created_before: datetime | None = None) -> bool:
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
        metadata: str,
        created_at: datetime,
        updated_at: datetime,
        messages: MessageChanges | None = None,
        images: ImageSet | None = None,
    ) -> None:
        """Write the session row, apply `messages` to its message rows and, with `images`, bring its
        photos to that set, in one transaction. The row goes first: the messages' and photos' foreign
        keys need it on Postgres."""
        async with session_scope(self._url) as session:
            session_ref = await SessionRepository(session).save(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "channel": channel,
                    "metadata": metadata,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
            if messages:
                await SessionMessageRepository(session).apply(
                    session_ref,
                    insert_rows=[_message_row(m) for m in messages.insert],
                    update_rows=[_message_row(m) for m in messages.update],
                    delete_ids=messages.delete,
                )
            if images is not None:
                await SessionImageRepository(session).sync(
                    session_ref,
                    images.keep,
                    [_as_row(image, created_at=time.time()) for image in images.add],
                )


def _as_dict(row: Any) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "user_id": row.user_id,
        "channel": row.channel,
        "metadata": row.session_metadata,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _as_row(image: StoredImage, *, created_at: float) -> SessionImage:
    """A photo to store; `SessionImageRepository.sync` sets whose it is."""
    return SessionImage(
        sha256=image.sha256,
        media_type=image.media_type,
        data=image.data,
        created_at=created_at,
    )


#: The fields each role may have beyond `role` and `content`, as the table's CHECKs hold them.
_ROLE_FIELDS = {
    "tool_call_id": ("tool_result",),
    "tool_calls": ("assistant",),
    "raw": ("assistant", "system"),
    "wire": ("assistant", "system"),
}


def _message_row(d: dict[str, Any]) -> dict[str, Any]:
    """A message dict as the columns of its row. A field its role cannot have is dropped with a warning
    (no code writes one): a CHECK failing would lose the whole save, and the turn with it."""
    role = d["role"]
    for name, roles in _ROLE_FIELDS.items():
        if d.get(name) and role not in roles:
            logger.warning("Not storing %s on a %s message", name, role)
    allowed = {name: role in roles for name, roles in _ROLE_FIELDS.items()}
    return {
        "id": d["id"],
        "role": role,
        "content": _text(d.get("content")) or "",
        "tool_call_id": _text(d.get("tool_call_id")) if allowed["tool_call_id"] else None,
        "is_error": bool(d.get("is_error", False)),
        "tool_calls": (d.get("tool_calls") or None) if allowed["tool_calls"] else None,
        "images": d.get("images") or None,
        "context": _text(d.get("context")),
        "raw": (d.get("raw") or None) if allowed["raw"] else None,
        "wire": _text(d.get("wire")) if allowed["wire"] else None,
        "created_at": d["created_at"],
    }


def _text(value: Any) -> str | None:
    """A text field as a TEXT column holds it: NULL when empty, and no NUL character, which Postgres
    refuses in text (U+FFFD takes its place, on both dialects alike)."""
    if not value:
        return None
    return str(value).replace("\x00", "\ufffd")


def _message_dict(row: SessionMessage) -> dict[str, Any]:
    """A row as `LLMMessage.to_dict` writes a message (only the fields that are set, in its order),
    with the row's `id` and `created_at`."""
    d: dict[str, Any] = {"role": row.role, "content": row.content}
    if row.tool_call_id:
        d["tool_call_id"] = row.tool_call_id
    if row.is_error:
        d["is_error"] = True
    if row.tool_calls:
        d["tool_calls"] = row.tool_calls
    if row.images:
        d["images"] = row.images
    if row.context:
        d["context"] = row.context
    if row.raw:
        d["raw"] = row.raw
    if row.wire:
        d["wire"] = row.wire
    return d | {"id": row.id, "created_at": row.created_at}
