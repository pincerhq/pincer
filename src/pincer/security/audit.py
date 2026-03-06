"""
Pincer Audit Logger — Compliance-grade event logging.

Logs every tool call, LLM request, file access, and network request.
Exportable as JSON/CSV for compliance audits.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

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
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class AuditLogger:
    """Async audit logger backed by SQLite with batched writes."""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        user_id TEXT NOT NULL,
        session_id TEXT,
        action TEXT NOT NULL,
        tool TEXT,
        input_summary TEXT,
        output_summary TEXT,
        approved INTEGER DEFAULT 1,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER,
        ip_address TEXT,
        channel TEXT,
        metadata_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
    CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
    CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool);
    """

    MAX_SUMMARY_LENGTH = 2000

    def __init__(self, db_path: str | Path = "data/audit.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._write_queue: asyncio.Queue[AuditEntry] = asyncio.Queue(maxsize=10000)
        self._flush_task: asyncio.Task[None] | None = None
        self._running = False

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(self.SCHEMA_SQL)
        await self._db.commit()
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def shutdown(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        await self._flush_pending()
        if self._db:
            await self._db.close()
            self._db = None

    async def log(self, entry: AuditEntry) -> None:
        """Queue an audit entry for batch writing (non-blocking)."""
        try:
            self._write_queue.put_nowait(entry)
        except asyncio.QueueFull:
            await self._flush_pending()
            await self._write_queue.put(entry)

    @asynccontextmanager
    async def track(
        self, user_id: str, action: AuditAction, **kwargs: Any
    ) -> AsyncIterator[AuditEntry]:
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
        assert self._db is not None

        conditions: list[str] = []
        params: list[Any] = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if action:
            conditions.append("action = ?")
            params.append(action.value)
        if tool:
            conditions.append("tool = ?")
            params.append(tool)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
            SELECT * FROM audit_log {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        async with self._db.execute(sql, params) as cursor:
            columns = [desc[0] for desc in cursor.description]
            rows = await cursor.fetchall()
            return [dict(zip(columns, row, strict=False)) for row in rows]

    async def export_json(
        self,
        output_path: str | Path,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """Export audit logs to JSON file. Returns number of records exported."""
        assert self._db is not None

        conditions: list[str] = []
        params: list[Any] = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM audit_log {where} ORDER BY timestamp ASC"

        output_path = Path(output_path)
        count = 0

        async with self._db.execute(sql, params) as cursor:
            columns = [desc[0] for desc in cursor.description]
            with open(output_path, "w") as f:
                f.write("[\n")
                first = True
                async for row in cursor:
                    if not first:
                        f.write(",\n")
                    record = dict(zip(columns, row, strict=False))
                    if record.get("metadata_json"):
                        try:
                            record["metadata"] = json.loads(
                                record.pop("metadata_json")
                            )
                        except json.JSONDecodeError:
                            record["metadata"] = {}
                    f.write(f"  {json.dumps(record, default=str)}")
                    first = False
                    count += 1
                f.write("\n]")

        return count

    async def get_stats(self, since: str | None = None) -> dict[str, Any]:
        """Get summary statistics for audit logs."""
        assert self._db is not None

        time_filter = f"WHERE timestamp >= '{since}'" if since else ""
        stats: dict[str, Any] = {}

        async with self._db.execute(
            f"SELECT COUNT(*) FROM audit_log {time_filter}"
        ) as cursor:
            row = await cursor.fetchone()
            stats["total_entries"] = row[0] if row else 0

        async with self._db.execute(
            f"SELECT action, COUNT(*) FROM audit_log {time_filter} "
            "GROUP BY action ORDER BY COUNT(*) DESC"
        ) as cursor:
            stats["by_action"] = {row[0]: row[1] async for row in cursor}

        async with self._db.execute(
            f"SELECT tool, COUNT(*) FROM audit_log {time_filter} "
            "WHERE tool IS NOT NULL GROUP BY tool ORDER BY COUNT(*) DESC"
        ) as cursor:
            stats["by_tool"] = {row[0]: row[1] async for row in cursor}

        async with self._db.execute(
            f"SELECT SUM(cost_usd) FROM audit_log {time_filter}"
        ) as cursor:
            row = await cursor.fetchone()
            stats["total_cost_usd"] = round(row[0] or 0.0, 6)

        async with self._db.execute(
            f"SELECT COUNT(*) FROM audit_log {time_filter} WHERE approved = 0"
        ) as cursor:
            row = await cursor.fetchone()
            stats["failed_actions"] = row[0] if row else 0

        return stats

    # ── Internal ──────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(2.0)
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        if self._db is None or self._write_queue.empty():
            return

        entries: list[AuditEntry] = []
        while not self._write_queue.empty():
            try:
                entries.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not entries:
            return

        sql = """
            INSERT INTO audit_log
            (timestamp, user_id, session_id, action, tool, input_summary,
             output_summary, approved, cost_usd, duration_ms, ip_address,
             channel, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (
                e.timestamp,
                e.user_id,
                e.session_id,
                e.action.value if isinstance(e.action, AuditAction) else e.action,
                e.tool,
                (e.input_summary or "")[: self.MAX_SUMMARY_LENGTH],
                (e.output_summary or "")[: self.MAX_SUMMARY_LENGTH],
                1 if e.approved else 0,
                e.cost_usd,
                e.duration_ms,
                e.ip_address,
                e.channel,
                json.dumps(e.metadata) if e.metadata else None,
            )
            for e in entries
        ]

        try:
            await self._db.executemany(sql, rows)
            await self._db.commit()
        except Exception:
            for entry in entries:
                try:
                    self._write_queue.put_nowait(entry)
                except asyncio.QueueFull:
                    break


_audit_logger: AuditLogger | None = None


async def get_audit_logger(db_path: str | Path = "data/audit.db") -> AuditLogger:
    """Singleton accessor for the audit logger."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger(db_path=db_path)
        await _audit_logger.initialize()
    return _audit_logger
