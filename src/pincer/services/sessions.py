"""Session persistence.

`SessionManager` owns the in-memory cache, trimming and onboarding state; this
is only the reading and writing of rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pincer.db.session import session_scope
from pincer.models.sessions import SessionImage
from pincer.repositories.sessions import SessionImageRepository, SessionRepository
from pincer.services.base import DatabaseService


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


class SessionService(DatabaseService):
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
            if row is None:
                return None
            # By the row's own key: a session left under the pre-unified key keeps its photos there
            photos = await SessionImageRepository(session).for_session(row.session_id)
            return _as_dict(row) | {"images": [StoredImage(i.sha256, i.media_type, i.data) for i in photos]}

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
        images: ImageSet | None = None,
    ) -> None:
        """Write the session row and, with `images`, bring its photos to that set, in one
        transaction. The row goes first: a photo's foreign key needs it on Postgres."""
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
            if images is not None:
                await SessionImageRepository(session).sync(
                    session_id,
                    images.keep,
                    [_as_row(session_id, image, created_at=time.time()) for image in images.add],
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


def _as_row(session_id: str, image: StoredImage, *, created_at: float) -> SessionImage:
    return SessionImage(
        session_id=session_id,
        sha256=image.sha256,
        media_type=image.media_type,
        data=image.data,
        created_at=created_at,
    )
