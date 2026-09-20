"""
Session management, stored through `pincer.services.sessions`.

A session = one conversation thread for one user on one channel.
Stores message history, supports trimming, and provides context for the agent.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pincer.db.engine import get_database_url, get_engine
from pincer.llm.base import LLMMessage, MessageRole
from pincer.services.sessions import SessionService

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _session_key(user_id: str, channel: str) -> str:
    return f"{channel}:{user_id}"


# Metadata keys used by the first-session onboarding flow.
ONBOARDING_COMPLETE_KEY = "onboarding_complete"
ONBOARDING_PROMPT_SENT_KEY = "onboarding_prompt_sent"
PROFILE_NAME_KEY = "name"
PROFILE_USE_CASE_KEY = "use_case"
PROFILE_LANGUAGE_KEY = "language"


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

    # ── Onboarding helpers ──────────────────────────────

    def is_fresh(self) -> bool:
        """True when the session has no non-system messages yet."""
        return not any(m.role != MessageRole.SYSTEM for m in self.messages)

    def is_onboarded(self) -> bool:
        return self.metadata.get(ONBOARDING_COMPLETE_KEY) == "true"

    def onboarding_prompt_already_sent(self) -> bool:
        return self.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"

    def mark_onboarding_prompt_sent(self) -> None:
        self.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"

    def mark_onboarded(
        self,
        *,
        name: str | None = None,
        use_case: str | None = None,
        language: str | None = None,
    ) -> None:
        self.metadata[ONBOARDING_COMPLETE_KEY] = "true"
        if name:
            self.metadata[PROFILE_NAME_KEY] = name.strip()[:100]
        if use_case:
            self.metadata[PROFILE_USE_CASE_KEY] = use_case.strip()[:300]
        if language:
            self.metadata[PROFILE_LANGUAGE_KEY] = language.strip()[:20]


class SessionManager:
    """Async SQLite-backed session store."""

    def __init__(self, db_path: Path, max_messages: int = 50) -> None:
        self._db_path = db_path
        self._max_messages = max_messages
        self._service: SessionService | None = None
        self._cache: dict[str, Session] = {}

    async def initialize(self) -> None:
        """Bring the database to head and attach the store.

        Production DB discipline (Sprint 7, T7.4): WAL and a 5s busy_timeout
        are set on every connection the shared engine opens (see
        `pincer.db.engine`), so this process and the tasks worker both wait on
        a lock rather than failing. Schema DDL lives in the Alembic migrations.
        """
        self._service = await SessionService.for_path(self._db_path)

    async def close(self) -> None:
        for session in self._cache.values():
            await self._persist(session)
        self._cache.clear()
        if self._service is not None:
            self._service = None
            await get_engine(get_database_url(self._db_path)).dispose()

    @property
    def _store(self) -> SessionService:
        if self._service is None:
            raise RuntimeError("SessionManager not initialized")
        return self._service

    async def get_or_create(
        self,
        user_id: str,
        channel: str,
        pincer_user_id: str = "",
    ) -> Session:
        """Get existing session or create a new one.

        If pincer_user_id is provided, it's used as the session key prefix
        for cross-channel continuity; otherwise falls back to channel:user_id.
        """
        key = _session_key(pincer_user_id, "unified") if pincer_user_id else _session_key(user_id, channel)

        if key in self._cache:
            return self._cache[key]

        row = await self._store.load(key, user_id=user_id, channel=channel)

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

        self._cache[key] = session
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

    async def clear(self, session: Session) -> None:
        """Clear all messages from a session."""
        session.messages.clear()
        session.updated_at = time.time()
        await self._persist(session)

    async def _persist(self, session: Session) -> None:
        """Write the session out."""
        await self._store.save(
            session_id=session.session_id,
            user_id=session.user_id,
            channel=session.channel,
            messages=json.dumps([m.to_dict() for m in session.messages]),
            metadata=json.dumps(session.metadata),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
