"""
Session management, stored through `pincer.services.sessions`.

A session = one conversation thread for one user on one channel.
Stores message history and provides context for the agent. A session is never cut by message
count: it is compacted by prompt tokens (`pincer.core.compaction`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pincer.llm.base import ImageContent, LLMMessage, MessageRole
from pincer.models.sessions import MEMORY_FREE_KEY
from pincer.services.onboarding import OnboardingService
from pincer.services.sessions import ImageSet, SessionService, StoredImage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from pincer.core.compaction import LastRequest

logger = logging.getLogger(__name__)

# Sessions held in memory at once; past it the least recently used is
# persisted and dropped, and reloads from the database on its next turn.
_DEFAULT_MAX_CACHED_SESSIONS = 1024
# Eviction tries this many sessions beyond the overflow per request, so a few
# that cannot be saved are stepped over without one request retrying them all.
_EVICTION_SPARE_ATTEMPTS = 2


def _session_key(user_id: str, channel: str) -> str:
    return f"{channel}:{user_id}"


def _resolve_session_key(user_id: str, channel: str, pincer_user_id: str = "", session_key: str | None = None) -> str:
    if session_key:
        return session_key
    if pincer_user_id:
        return _session_key(pincer_user_id, "unified")
    return _session_key(user_id, channel)


def channel_session_key(user_id: str, channel: str) -> str:
    """A user's main session on a channel (no conversation, no identity)."""
    return _session_key(user_id, channel)


def conversation_key(user_id: str, channel: str, conversation_id: str) -> str:
    """One conversation of a user's, nested under their channel session."""
    return f"{_session_key(user_id, channel)}:{conversation_id}"


# Session metadata the onboarding flow wrote before `pincer_onboarding`; now
# only read, for a user with no row there.
ONBOARDING_COMPLETE_KEY = "onboarding_complete"
ONBOARDING_PROMPT_SENT_KEY = "onboarding_prompt_sent"


@dataclass
class Session:
    """In-memory representation of a conversation session."""

    session_id: str
    user_id: str
    channel: str
    messages: list[LLMMessage] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pincer_user_id: str = ""
    # Hash of the messages and metadata as last saved (or loaded), so an
    # eviction can skip a session that has not changed.
    _saved_hash: int | None = field(default=None, init=False, repr=False, compare=False)
    # Set once an eviction's save has failed and been warned about, so a
    # session that keeps failing is not logged with a traceback every request.
    _eviction_failed: bool = field(default=False, init=False, repr=False, compare=False)
    # The hashes of the photos the image store holds for this session, as last saved or loaded;
    # `_persist` brings the store to the photos the messages hold now.
    _synced_images: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)
    # What the latest LLM request of the session's turn sent, for compaction; never saved
    last_request: LastRequest | None = field(default=None, init=False, repr=False, compare=False)

    # ── Onboarding (legacy flags, see ONBOARDING_COMPLETE_KEY) ──────────

    def is_fresh(self) -> bool:
        """True when the session has no non-system messages yet."""
        return not any(m.role != MessageRole.SYSTEM for m in self.messages)

    def is_onboarded(self) -> bool:
        return self.metadata.get(ONBOARDING_COMPLETE_KEY) == "true"

    def onboarding_prompt_already_sent(self) -> bool:
        return self.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"

    def is_memory_free(self) -> bool:
        """True once a turn without long-term memory has run in the session."""
        return self.metadata.get(MEMORY_FREE_KEY) == "true"

    def mark_memory_free(self) -> None:
        """Flag the session memory-free; saved with its next message."""
        self.metadata[MEMORY_FREE_KEY] = "true"


class SessionManager:
    """Async SQLite-backed session store."""

    def __init__(self, db_path: Path, max_cached: int = _DEFAULT_MAX_CACHED_SESSIONS) -> None:
        self._db_path = db_path
        self._max_cached = max_cached
        self._service: SessionService | None = None
        self._onboarding: OnboardingService | None = None
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._turn_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def turn(
        self, user_id: str, channel: str, session_key: str | None = None, pincer_user_id: str = ""
    ) -> AsyncIterator[Session]:
        """Run one agent turn on the session: serialize turns, pin the session while they are
        active or queued, and yield it. Every message of the turn is saved as it is added."""
        key = _resolve_session_key(user_id, channel, pincer_user_id, session_key)
        async with self._locked(key):
            yield await self.get_or_create(user_id, channel, pincer_user_id, session_key)

    @asynccontextmanager
    async def _locked(self, key: str) -> AsyncIterator[None]:
        """Hold the session's turn lock, pinning it in the cache while held or awaited."""
        lock, users = self._turn_locks.get(key, (asyncio.Lock(), 0))
        self._turn_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current_lock, users = self._turn_locks[key]
            if users == 1:
                del self._turn_locks[key]
            else:
                self._turn_locks[key] = (current_lock, users - 1)

    async def compact(
        self,
        session: Session,
        prefix: list[LLMMessage],
        replacement: LLMMessage,
        kept_ok: Callable[[list[LLMMessage]], bool] | None = None,
    ) -> bool:
        """Replace the session's oldest messages `prefix` by `replacement`, between turns.

        Applies only while `prefix` is still the start of the cached session's history (the
        same message objects), so a turn, repair, clear or cut since it was chosen wins; and
        only if `kept_ok` accepts the messages that would follow the replacement, judged under
        the turn lock, when no turn can add to them. True if the history changed.
        """
        return await self._replace_prefix(session, prefix, replacement, kept_ok)

    async def cut(self, session: Session, prefix: list[LLMMessage]) -> bool:
        """Drop the session's oldest messages `prefix` (the emergency cut), between turns, on the
        same terms as `compact`. The photos only they showed are deleted with them."""
        return await self._replace_prefix(session, prefix, None, None)

    async def _replace_prefix(
        self,
        session: Session,
        prefix: list[LLMMessage],
        replacement: LLMMessage | None,
        kept_ok: Callable[[list[LLMMessage]], bool] | None,
    ) -> bool:
        async with self._locked(session.session_id):
            if self._cache.get(session.session_id) is not session:
                return False
            head = session.messages[: len(prefix)]
            if len(head) != len(prefix) or any(a is not b for a, b in zip(head, prefix, strict=True)):
                return False
            if kept_ok is not None and not kept_ok(session.messages[len(prefix) :]):
                return False
            session.messages = [*([replacement] if replacement else []), *session.messages[len(prefix) :]]
            session.updated_at = time.time()
            await self._persist(session)
            return True

    async def initialize(self) -> None:
        """Bring the database to head and attach the store.

        Production DB discipline (Sprint 7, T7.4): WAL and a 5s busy_timeout
        are set on every connection the shared engine opens (see
        `pincer.db.engine`), so this process and the tasks worker both wait on
        a lock rather than failing. Schema DDL lives in the Alembic migrations.
        """
        self._service = await SessionService.for_path(self._db_path)
        self._onboarding = await OnboardingService.for_path(self._db_path)

    async def close(self) -> None:
        for session in list(self._cache.values()):
            await self._persist(session, only_if_changed=True)
        self._cache.clear()
        # The engine is shared; the process disposes it at shutdown.
        self._service = None
        self._onboarding = None

    @property
    def _store(self) -> SessionService:
        if self._service is None:
            raise RuntimeError("SessionManager not initialized")
        return self._service

    @property
    def onboarding(self) -> OnboardingService:
        """Each user's onboarding state per channel, in the same database."""
        if self._onboarding is None:
            raise RuntimeError("SessionManager not initialized")
        return self._onboarding

    async def has_stored_sessions(self, user_id: str, channel: str, *, created_before: float | None = None) -> bool:
        """True when the user has any session stored on the channel, conversations included
        (memory-free ones aside, see `Session.is_memory_free`); with `created_before`,
        only one created before that time."""
        return await self._store.has_any(user_id, channel, created_before=created_before)

    async def get_or_create(
        self,
        user_id: str,
        channel: str,
        pincer_user_id: str = "",
        session_key: str | None = None,
    ) -> Session:
        """Get existing session or create a new one.

        If pincer_user_id is provided, it's used as the session key prefix
        for cross-channel continuity; otherwise falls back to channel:user_id.
        ``session_key`` overrides both with an exact key (a phone call's
        session, which must not depend on how its user was resolved).
        """
        key = _resolve_session_key(user_id, channel, pincer_user_id, session_key)

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        row = await self._store.load(key, user_id=user_id, channel=channel, legacy_fallback=not session_key)

        if row:
            photos = {
                photo.sha256: ImageContent(
                    data=base64.b64encode(photo.data).decode(), media_type=photo.media_type, sha256=photo.sha256
                )
                for photo in row["images"]
            }
            stored = json.loads(row["messages"])
            missing = {ref["sha256"] for m in stored for ref in m.get("images", [])} - photos.keys()
            if missing:
                logger.warning("Session %s refers to %d photo(s) its image store lacks; left out", key, len(missing))
            messages = [LLMMessage.from_dict(m, photos) for m in stored]
            session = Session(
                session_id=key,
                user_id=user_id,
                channel=channel,
                messages=messages,
                metadata=json.loads(row["metadata"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                pincer_user_id=pincer_user_id,
            )
        else:
            session = Session(
                session_id=key,
                user_id=user_id,
                channel=channel,
                pincer_user_id=pincer_user_id,
            )

        session._saved_hash = hash(_serialized(session))
        if row and row["session_id"] == key:  # a session left under the old key stores its photos anew
            session._synced_images = frozenset(photo.sha256 for photo in row["images"])
        self._cache[key] = session
        await self._evict_overflow(keep=key)
        return session

    async def add_message(self, session: Session, message: LLMMessage) -> None:
        """Add a message to the session and save it."""
        session.messages.append(message)
        session.updated_at = time.time()
        await self._persist(session)

    async def append_note(self, session_key: str, user_id: str, channel: str, text: str) -> None:
        """Add an assistant message to a session from outside any turn (a
        phone call's final report), in turn order with the user's own turns.

        A trailing assistant message is fine: the Anthropic serializer merges
        consecutive same-role messages, and OpenAI accepts them as they are.
        """
        async with self.turn(user_id, channel, session_key=session_key) as session:
            await self.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content=text))

    async def save(self, session: Session) -> None:
        """Write the session out after its messages were changed in place."""
        await self._persist(session)

    async def clear(self, session: Session) -> None:
        """Clear all messages from a session."""
        session.messages.clear()
        session.updated_at = time.time()
        await self._persist(session, force_images=True)

    async def evict(self, key: str) -> None:
        """Persist and drop one session from the cache (a phone call's, at
        hangup — a key per call would otherwise grow the cache forever).

        The session stays cached until its save lands, so a request for it
        meanwhile gets this object rather than the row as it was before.
        """
        session = self._cache.get(key)
        if session is None:
            return
        await self._persist(session, only_if_changed=True)
        if self._cache.get(key) is session:
            del self._cache[key]

    async def _evict_overflow(self, keep: str) -> None:
        """Persist and drop least recently used sessions past the cap.

        Runs inside the request for `keep`, which is never evicted here. A
        session whose save fails is logged and kept, moved to the recent end
        so the next one in line is tried; the request goes on, and the cache
        stays over the cap until later evictions succeed. A session hit or
        replaced while its save was awaited is kept too. At most the overflow
        plus `_EVICTION_SPARE_ATTEMPTS` sessions are tried per request.
        """
        # Known race: a turn still holding a session evicted after its save
        # keeps writing that object while the next request loads a second
        # copy, and the last write wins. It takes more than `max_cached`
        # other keys within one turn, so only a client minting new
        # conversation ids gets near it.
        attempts = len(self._cache) - self._max_cached + _EVICTION_SPARE_ATTEMPTS
        while len(self._cache) > self._max_cached and attempts > 0:
            attempts -= 1
            key, session = next(iter(self._cache.items()))
            if key == keep:
                return  # everything older was tried
            if key in self._turn_locks:
                self._cache.move_to_end(key)
                continue
            try:
                await self._persist(session, only_if_changed=True)
            except Exception:
                if session._eviction_failed:
                    logger.debug("Session %s still cannot be saved to evict it", key)
                else:
                    logger.warning("Could not save session %s to evict it; keeping it cached", key, exc_info=True)
                    session._eviction_failed = True
                if self._cache.get(key) is session:
                    self._cache.move_to_end(key)
                continue
            session._eviction_failed = False
            if (
                key not in self._turn_locks
                and next(iter(self._cache), None) == key
                and self._cache[key] is session
                and not _changed(session)
            ):
                del self._cache[key]

    async def _persist(self, session: Session, *, only_if_changed: bool = False, force_images: bool = False) -> None:
        """Write the session out (with ``only_if_changed``, only when its
        messages or metadata differ from what was last saved or loaded).

        Its photos follow its messages: new ones are stored, and the ones no message holds any
        more are deleted, whichever code removed those messages. ``force_images`` also deletes
        what an earlier session of this process left behind (a clear).
        """
        state = _serialized(session)
        if only_if_changed and hash(state) == session._saved_hash:
            return
        messages, metadata = state
        images = _image_set(session, force=force_images)
        await self._store.save(
            session_id=session.session_id,
            user_id=session.user_id,
            channel=session.channel,
            messages=messages,
            metadata=metadata,
            created_at=session.created_at,
            updated_at=session.updated_at,
            images=images,
        )
        session._saved_hash = hash(state)
        if images is not None:
            session._synced_images = images.keep


def _serialized(session: Session) -> tuple[str, str]:
    """The session's messages and metadata as they are stored."""
    return json.dumps([m.to_dict() for m in session.messages]), json.dumps(session.metadata)


def _image_set(session: Session, *, force: bool) -> ImageSet | None:
    """The photos the session's messages hold, if they differ from what its image store holds."""
    held: dict[str, ImageContent] = {}
    for message in session.messages:
        for image in message.images:
            if image.sha256:
                held.setdefault(image.sha256, image)
    keep = frozenset(held)
    if keep == session._synced_images and not force:
        return None
    add = tuple(
        StoredImage(sha, image.media_type, base64.b64decode(image.data))
        for sha, image in held.items()
        if sha not in session._synced_images
    )
    return ImageSet(keep, add)


def _changed(session: Session) -> bool:
    return hash(_serialized(session)) != session._saved_hash
