"""
In-memory state management for the Pomodoro MCP server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class SessionStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BreakType(StrEnum):
    SHORT = "short"
    LONG = "long"


@dataclass
class Session:
    id: str
    session_type: str  # "pomodoro" | "short_break" | "long_break"
    description: str
    tags: list[str]
    duration_seconds: int
    status: SessionStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    paused_at: datetime | None = None
<<<<<<< HEAD
    paused_duration: int = 0   # cumulative seconds spent paused
=======
    paused_duration: int = 0  # cumulative seconds spent paused
>>>>>>> dev


class PomodoroStore:
    """Thread-safe (asyncio) in-memory store for sessions."""

    def __init__(self) -> None:
        self._active: Session | None = None
        self._history: list[Session] = []

    # ── Active session ────────────────────────────────────────────────────

    @property
    def active_session(self) -> Session | None:
        return self._active

    def set_active(self, session: Session) -> None:
        self._active = session

    def complete_active(self) -> None:
        """Move active session to history and clear the slot."""
        if self._active:
            self._history.append(self._active)
            self._active = None

    # ── Queries ───────────────────────────────────────────────────────────

    def last_pomodoro(self) -> Session | None:
        """Return the most recent completed Pomodoro session."""
        for s in reversed(self._history):
            if s.session_type == "pomodoro" and s.status == SessionStatus.COMPLETED:
                return s
        return None

    def history(
        self,
        limit: int = 10,
        session_type: str | None = None,
        tag: str | None = None,
    ) -> list[Session]:
        results = list(reversed(self._history))

        if session_type:
            results = [s for s in results if s.session_type == session_type]
        if tag:
            results = [s for s in results if tag in s.tags]

        return results[:limit]

    def daily_stats(self) -> dict:
        today = datetime.now(UTC).date()

        today_sessions = [s for s in self._history if s.started_at and s.started_at.date() == today]

        pomodoros = [s for s in today_sessions if s.session_type == "pomodoro"]
        completed = [s for s in pomodoros if s.status == SessionStatus.COMPLETED]
        cancelled = [s for s in pomodoros if s.status == SessionStatus.CANCELLED]
        breaks = [
            s
            for s in today_sessions
            if s.session_type in ("short_break", "long_break") and s.status == SessionStatus.COMPLETED
        ]

        focus_seconds = sum(s.duration_seconds for s in completed)

        return {
            "date": today.isoformat(),
            "pomodoros_completed": len(completed),
            "pomodoros_cancelled": len(cancelled),
            "breaks_taken": len(breaks),
            "total_focus_minutes": focus_seconds // 60,
            "goal_progress": f"{len(completed)}/8",
        }
