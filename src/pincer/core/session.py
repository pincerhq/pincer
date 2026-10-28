"""
Session management, stored through `pincer.services.sessions`.

A session = one conversation thread for one user on one channel.
Stores message history, supports trimming, and provides context for the agent.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pincer.llm.base import LLMMessage, MessageRole
from pincer.services.onboarding import OnboardingService
from pincer.services.sessions import SessionService

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Sessions held in memory at once; past it the least recently used is
# persisted and dropped, and reloads from the database on its next turn.
_DEFAULT_MAX_CACHED_SESSIONS = 1024
# Eviction tries this many sessions beyond the overflow per request, so a few
# that cannot be saved are stepped over without one request retrying them all.
_EVICTION_SPARE_ATTEMPTS = 2


def _session_key(user_id: str, channel: str) -> str:
    return f"{channel}:{user_id}"


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

    # ── Onboarding (legacy flags, see ONBOARDING_COMPLETE_KEY) ──────────

    def is_fresh(self) -> bool:
        """True when the session has no non-system messages yet."""
        return not any(m.role != MessageRole.SYSTEM for m in self.messages)

    def is_onboarded(self) -> bool:
        return self.metadata.get(ONBOARDING_COMPLETE_KEY) == "true"

    def onboarding_prompt_already_sent(self) -> bool:
        return self.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"


class SessionManager:
    """Async SQLite-backed session store."""

    def __init__(self, db_path: Path, max_messages: int = 50, max_cached: int = _DEFAULT_MAX_CACHED_SESSIONS) -> None:
        self._db_path = db_path
        self._max_messages = max_messages
        self._max_cached = max_cached
        self._service: SessionService | None = None
        self._onboarding: OnboardingService | None = None
        self._cache: OrderedDict[str, Session] = OrderedDict()

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

    async def has_stored_sessions(self, user_id: str, channel: str) -> bool:
        """True when the user has any session stored on the channel, conversations included."""
        return await self._store.has_any(user_id, channel)

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
        if session_key:
            key = session_key
        elif pincer_user_id:
            key = _session_key(pincer_user_id, "unified")
        else:
            key = _session_key(user_id, channel)

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        row = await self._store.load(key, user_id=user_id, channel=channel, legacy_fallback=not session_key)

        if row:
            messages = [LLMMessage.from_dict(m) for m in json.loads(row["messages"])]
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
        self._cache[key] = session
        await self._evict_overflow(keep=key)
        return session

    async def add_message(self, session: Session, message: LLMMessage) -> None:
        """Add a message to session and auto-trim if needed."""
        session.messages.append(message)
        session.updated_at = time.time()

        if len(session.messages) > self._max_messages:
            system_msgs = [m for m in session.messages if m.role == MessageRole.SYSTEM]
            other_msgs = [m for m in session.messages if m.role != MessageRole.SYSTEM]
            start = len(other_msgs) - (self._max_messages - len(system_msgs))
            # Never start on a tool_result — it would be orphaned without its tool_use
            while start < len(other_msgs) and other_msgs[start].role == MessageRole.TOOL_RESULT:
                start += 1
            # Never leave an orphaned tool_use right before the trim boundary
            if start > 0 and other_msgs[start - 1].role == MessageRole.ASSISTANT and other_msgs[start - 1].tool_calls:
                start -= 1
            trimmed = other_msgs[start:]
            session.messages = system_msgs + trimmed
            logger.debug(
                "Session %s trimmed to %d messages",
                session.session_id,
                len(session.messages),
            )

        await self._persist(session)

    async def save(self, session: Session) -> None:
        """Write the session out after its messages were changed in place."""
        await self._persist(session)

    async def clear(self, session: Session) -> None:
        """Clear all messages from a session."""
        session.messages.clear()
        session.updated_at = time.time()
        await self._persist(session)

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
            if next(iter(self._cache), None) == key and self._cache[key] is session and not _changed(session):
                del self._cache[key]

    async def _persist(self, session: Session, *, only_if_changed: bool = False) -> None:
        """Write the session out (with ``only_if_changed``, only when its
        messages or metadata differ from what was last saved or loaded)."""
        state = _serialized(session)
        if only_if_changed and hash(state) == session._saved_hash:
            return
        messages, metadata = state
        await self._store.save(
            session_id=session.session_id,
            user_id=session.user_id,
            channel=session.channel,
            messages=messages,
            metadata=metadata,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
        session._saved_hash = hash(state)


def _serialized(session: Session) -> tuple[str, str]:
    """The session's messages and metadata as they are stored."""
    return json.dumps([m.to_dict() for m in session.messages]), json.dumps(session.metadata)


def _changed(session: Session) -> bool:
    return hash(_serialized(session)) != session._saved_hash
