"""
Pincer Audit Logger — Compliance-grade event logging.

Logs every tool call, LLM request, file access, and network request.
Exportable as JSON/CSV for compliance audits.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.services.audit import MAX_SUMMARY_LENGTH, AuditService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class AuditAction(StrEnum):
    TOOL_CALL = "tool_call"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    NETWORK_REQUEST = "network_request"
    SKILL_EXECUTE = "skill_execute"
    AUTH_ATTEMPT = "auth_attempt"
    CONFIG_CHANGE = "config_change"
    BUDGET_ALERT = "budget_alert"
    RATE_LIMIT_HIT = "rate_limit_hit"
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENT = "message_sent"
    ERROR = "error"
    # Sprint 7: Voice calling events
    VOICE_CALL_START = "voice_call_start"
    VOICE_CALL_END = "voice_call_end"
    VOICE_TOOL_CALL = "voice_tool_call"
    VOICE_TRANSFER = "voice_transfer"
    # Sprint 8 (T8.3): outbound dial refused by the abuse gate
    VOICE_CALL_BLOCKED = "voice_call_blocked"
    # Sprint 0 (DACH): GDPR storage-limitation purge
    RETENTION_PURGE = "retention_purge"
    # Sprint 13: every call-thread status transition (§5)
    VOICE_THREAD_LIFECYCLE = "voice_thread_lifecycle"
    # Sprint 15: one row per live listen-in session (who listened to which call, how long)
    LISTEN_IN_SESSION = "listen_in_session"


@dataclass
class AuditEntry:
    user_id: str
    action: AuditAction
    tool: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    approved: bool = True
    cost_usd: float = 0.0
    duration_ms: int | None = None
    ip_address: str | None = None
    channel: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class AuditLogger:
    """Async audit logger backed by SQLite with batched writes."""

    #: Kept as a class attribute for callers that read it.
    MAX_SUMMARY_LENGTH = MAX_SUMMARY_LENGTH

    def __init__(self, db_path: str | Path = "data/pincer.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._service: AuditService | None = None
        self._write_queue: asyncio.Queue[AuditEntry] = asyncio.Queue(maxsize=10000)
        self._flush_task: asyncio.Task[None] | None = None
        self._running = False

    async def initialize(self) -> None:
        self._service = await AuditService.for_path(self.db_path)
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def shutdown(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        await self._flush_pending()
        self._service = None

    @property
    def service(self) -> AuditService:
        if self._service is None:
            raise RuntimeError("AuditLogger not initialized")
        return self._service

    async def log(self, entry: AuditEntry) -> None:
        """Queue an audit entry for batch writing (non-blocking)."""
        try:
            self._write_queue.put_nowait(entry)
        except asyncio.QueueFull:
            await self._flush_pending()
            await self._write_queue.put(entry)

    @asynccontextmanager
    async def track(self, user_id: str, action: AuditAction, **kwargs: Any) -> AsyncIterator[AuditEntry]:
        """Context manager that auto-tracks duration and errors."""
        entry = AuditEntry(user_id=user_id, action=action, **kwargs)
        start = time.monotonic()
        try:
            yield entry
        except Exception as e:
            entry.output_summary = f"ERROR: {type(e).__name__}: {str(e)[:500]}"
            entry.approved = False
            raise
        finally:
            entry.duration_ms = int((time.monotonic() - start) * 1000)
            await self.log(entry)

    async def query(
        self,
        user_id: str | None = None,
        action: AuditAction | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query audit logs with filters."""
        return await self.service.query(user_id, action.value if action else None, tool, since, until, limit, offset)

    async def count(
        self,
        user_id: str | None = None,
        action: AuditAction | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """Count audit log entries matching filters."""
        return await self.service.count(user_id, action.value if action else None, tool, since, until)

    async def export_json(
        self,
        output_path: str | Path,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """Export audit logs to JSON file. Returns number of records exported."""
        return await self.service.export_json(output_path, user_id, since, until)

    async def get_stats(self, since: str | None = None) -> dict[str, Any]:
        """Get summary statistics for audit logs."""
        return await self.service.stats(since)

    # ── Internal ──────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(2.0)
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        if self._service is None or self._write_queue.empty():
            return

        entries: list[AuditEntry] = []
        while not self._write_queue.empty():
            try:
                entries.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not entries:
            return

        try:
            await self._service.add_batch(entries)
        except Exception:
            # Keep them for the next flush rather than losing the evidence.
            for entry in entries:
                try:
                    self._write_queue.put_nowait(entry)
                except asyncio.QueueFull:
                    break


_audit_logger: AuditLogger | None = None


async def get_audit_logger(db_path: str | Path | None = None) -> AuditLogger:
    """Singleton accessor for the audit logger."""
    global _audit_logger
    if _audit_logger is None or not _audit_logger._running:
        if _audit_logger is None:
            if db_path is None:
                try:
                    from pincer.config import get_settings_relaxed

                    db_path = get_settings_relaxed().db_path
                except Exception:
                    db_path = Path("data/pincer.db")
            _audit_logger = AuditLogger(db_path=db_path)
        await _audit_logger.initialize()
    return _audit_logger
