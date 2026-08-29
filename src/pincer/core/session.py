"""
Session management with SQLite storage.

A session = one conversation thread for one user on one channel.
Stores message history, supports trimming, and provides context for the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

from pincer.db import ensure_schema_current
from pincer.llm.base import LLMMessage, MessageRole

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
        self._db: aiosqlite.Connection | None = None
        self._cache: dict[str, Session] = {}

    async def initialize(self) -> None:
        await asyncio.to_thread(ensure_schema_current, self._db_path)
        self._db = await aiosqlite.connect(str(self._db_path))
        # Production DB discipline (Sprint 7, T7.4): WAL is a persistent,
        # database-level setting — setting it here (the primary owner of
        # pincer.db) covers every later connection from any module or the
        # tasks-worker process. busy_timeout is per-connection: this long-lived
        # writer must wait, not fail, when a short-lived reader holds the lock.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_user
            ON sessions(user_id, channel)
        """)
        await self._db.commit()

    async def close(self) -> None:
        for session in self._cache.values():
            await self._persist(session)
        if self._db:
            await self._db.close()
            self._db = None
        self._cache.clear()

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

        assert self._db is not None
        async with self._db.execute(
            "SELECT session_id, messages, metadata, created_at, updated_at "
            "FROM sessions WHERE session_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            # Fallback: try the old channel:user_id key for backward compat
            fallback_key = _session_key(user_id, channel)
            if fallback_key != key:
                async with self._db.execute(
                    "SELECT session_id, messages, metadata, created_at, updated_at "
                    "FROM sessions WHERE user_id = ? AND channel = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (user_id, channel),
                ) as cursor:
                    row = await cursor.fetchone()

        if row:
            messages = [LLMMessage.from_dict(m) for m in json.loads(row[1])]
            session = Session(
                session_id=key,
                user_id=user_id,
                channel=channel,
                messages=messages,
                metadata=json.loads(row[2]),
                created_at=row[3],
                updated_at=row[4],
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
        """Write session to SQLite."""
        assert self._db is not None
        messages_json = json.dumps([m.to_dict() for m in session.messages])
        metadata_json = json.dumps(session.metadata)
        await self._db.execute(
            """INSERT INTO sessions
               (session_id, user_id, channel, messages, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   messages = excluded.messages,
                   metadata = excluded.metadata,
                   updated_at = excluded.updated_at""",
            (
                session.session_id,
                session.user_id,
                session.channel,
                messages_json,
                metadata_json,
                session.created_at,
                session.updated_at,
            ),
        )
        await self._db.commit()
