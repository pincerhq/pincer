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
from pincer.models.sessions import ROLE_FIELDS, SessionImage, SessionMessage
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

    async def edit(self, session_id: str, channel_message_id: str, content: str, *, at: datetime) -> bool:
        """Rewrite the text of the user message the messenger calls `channel_message_id`, edited there
        `at`. False when the session or the message is not there. Go through `SessionManager.edit_message`,
        which keeps its cached copy of the session from writing the old text back."""
        async with session_scope(self._url) as session:
            row = await SessionRepository(session).get_by(session_id=session_id)
            if row is None:
                return False
            return await SessionMessageRepository(session).edit(row.id, channel_message_id, _text(content) or "", at=at)

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
                await SessionMessageRepository(session).write_changes(
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


#: The fields of `ROLE_FIELDS` a save writes (`channel_message_id` and `updated_at` belong to an edit)
_SAVED_ROLE_FIELDS = ("tool_call_id", "tool_calls", "raw", "wire", "context")
#: Of those, the ones that are JSON rather than text
_JSON_FIELDS = frozenset({"tool_calls", "raw"})


def _message_row(message: dict[str, Any]) -> dict[str, Any]:
    """A message dict as the columns of its row. A field its role cannot have (`ROLE_FIELDS`) is dropped
    with a warning (no code writes one): a CHECK failing would lose the whole save, and the turn with it."""
    role = message["role"]
    role_fields: dict[str, Any] = {}
    for name in _SAVED_ROLE_FIELDS:
        value = message.get(name)
        if value and role not in ROLE_FIELDS[name]:
            logger.warning("Not storing %s on a %s message", name, role)
            value = None
        role_fields[name] = (storable_json(value) if value else None) if name in _JSON_FIELDS else _text(value)
    return {
        "id": message["id"],
        "role": role,
        "content": _text(message.get("content")) or "",
        "is_error": bool(message.get("is_error", False)),
        "images": message.get("images") or None,
        **role_fields,
        "created_at": message["created_at"],
    }


def storable_text(value: str) -> str:
    """`value` as a TEXT column holds it on both dialects alike. A NUL character is stripped: Postgres
    text takes none. A lone surrogate (half of an emoji a messenger cut) becomes `?`: UTF-8 cannot encode
    it, and the insert would fail on every save of the session. The JSON list escaped both."""
    if "\x00" in value:
        value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "replace").decode("utf-8")
    return value


def storable_json(value: Any) -> Any:
    """`value` with every string a JSON column can hold: a lone surrogate becomes `?` (Postgres refuses
    one in `json`, which keeps a NUL escaped). The same object when nothing needed it, else a copy."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, list):
        items = [storable_json(item) for item in value]
        return value if all(a is b for a, b in zip(items, value, strict=True)) else items
    if isinstance(value, dict):
        pairs = {storable_json(key): storable_json(item) for key, item in value.items()}
        same = all(a is b for a, b in zip(pairs.values(), value.values(), strict=True)) and list(pairs) == list(value)
        return value if same else pairs
    return value


def _text(value: Any) -> str | None:
    """A text field as its TEXT column holds it (`storable_text`), NULL when empty."""
    if not value:
        return None
    return storable_text(str(value)) or None


def _message_dict(row: SessionMessage) -> dict[str, Any]:
    """A row as `LLMMessage.to_dict` writes a message (only the fields that are set, in its order),
    with the row's `id` and `created_at`."""
    message: dict[str, Any] = {"role": row.role, "content": row.content}
    if row.tool_call_id:
        message["tool_call_id"] = row.tool_call_id
    if row.is_error:
        message["is_error"] = True
    for name in ("tool_calls", "images", "context", "raw", "wire"):
        if value := getattr(row, name):
            message[name] = value
    return message | {"id": row.id, "created_at": row.created_at}
