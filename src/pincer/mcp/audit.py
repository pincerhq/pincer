"""
MCP audit log helpers.

MCP events are logged through Pincer's existing AuditLogger using
AuditAction.TOOL_CALL (for tool calls) and dedicated MCP action types.
All MCP server events are tagged with metadata["mcp_server"] for filtering.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pincer.security.audit import AuditAction, AuditEntry

if TYPE_CHECKING:
    from pincer.mcp.security import MCPSecurityGate
    from pincer.security.audit import AuditLogger

# Max chars to log for argument values (privacy + log size)
_MAX_ARG_VALUE_LEN = 200
_MAX_ARG_TOTAL_LEN = 500


def _truncate_args(
    arguments: dict[str, Any] | None,
    redactor: MCPSecurityGate | None = None,
) -> str | None:
    """Serialize arguments with truncation and optional credential redaction."""
    if not arguments:
        return None
    import json

    try:
        raw = json.dumps(arguments)
        if len(raw) > _MAX_ARG_TOTAL_LEN:
            raw = raw[:_MAX_ARG_TOTAL_LEN] + "..."
        if redactor:
            raw = redactor.redact_sensitive(raw)
        return raw
    except Exception:
        return str(arguments)[:_MAX_ARG_TOTAL_LEN]


class MCPAuditLogger:
    """
    Thin wrapper that emits MCP events into Pincer's AuditLogger.

    All MCP actions are logged as user_id="mcp:<server_name>" so they
    can be queried separately: `pincer audit --user mcp:github`
    """

    def __init__(
        self,
        audit_log: AuditLogger | None,
        security_gate: MCPSecurityGate | None = None,
    ) -> None:
        self._log = audit_log
        self._security_gate = security_gate
        self._timers: dict[str, float] = {}  # call_key → start_time

    async def log_connect(self, server: str, *, success: bool, error: str | None = None) -> None:
        if not self._log:
            return
        await self._log.log(AuditEntry(
            user_id=f"mcp:{server}",
            action=AuditAction.TOOL_CALL,
            tool=f"_mcp_connect:{server}",
            input_summary="transport: connect",
            output_summary=(
                "connected" if success else f"failed: {(error or '')[:200]}"
            ),
            approved=success,
            metadata={"mcp_server": server, "mcp_event": "connect"},
        ))

    async def log_disconnect(self, server: str, *, error: str | None = None) -> None:
        if not self._log:
            return
        await self._log.log(AuditEntry(
            user_id=f"mcp:{server}",
            action=AuditAction.TOOL_CALL,
            tool=f"_mcp_disconnect:{server}",
            output_summary="disconnected" if not error else f"error: {error[:200]}",
            metadata={"mcp_server": server, "mcp_event": "disconnect"},
        ))

    def start_call(self, server: str, tool: str, call_key: str) -> None:
        """Record the start time of a tool call for duration tracking."""
        self._timers[call_key] = time.monotonic()

    async def log_tool_call(
        self,
        server: str,
        tool: str,
        call_key: str,
        arguments: dict[str, Any] | None,
        *,
        success: bool,
        output_length: int | None = None,
        error: str | None = None,
    ) -> None:
        if not self._log:
            self._timers.pop(call_key, None)
            return

        start = self._timers.pop(call_key, None)
        duration_ms = int((time.monotonic() - start) * 1000) if start else None

        await self._log.log(AuditEntry(
            user_id=f"mcp:{server}",
            action=AuditAction.TOOL_CALL,
            tool=tool,
            input_summary=_truncate_args(arguments, self._security_gate),
            output_summary=(
                f"length:{output_length}"
                if success
                else "error: " + (
                    self._security_gate.redact_sensitive((error or "")[:200])
                    if self._security_gate
                    else (error or "")[:200]
                )
            ),
            approved=success,
            duration_ms=duration_ms,
            metadata={
                "mcp_server": server,
                "mcp_event": "tool_call",
                "mcp_tool": tool,
                "status": "success" if success else "error",
            },
        ))

    async def log_health_check(self, server: str, *, healthy: bool) -> None:
        if not self._log:
            return
        await self._log.log(AuditEntry(
            user_id=f"mcp:{server}",
            action=AuditAction.TOOL_CALL,
            tool=f"_mcp_health:{server}",
            output_summary="healthy" if healthy else "unhealthy",
            metadata={"mcp_server": server, "mcp_event": "health_check"},
        ))

    async def log_reconnect(
        self, server: str, *, attempt: int, success: bool, error: str | None = None
    ) -> None:
        if not self._log:
            return
        await self._log.log(AuditEntry(
            user_id=f"mcp:{server}",
            action=AuditAction.TOOL_CALL,
            tool=f"_mcp_reconnect:{server}",
            input_summary=f"attempt {attempt}",
            output_summary=(
                "reconnected" if success else f"failed: {(error or '')[:200]}"
            ),
            approved=success,
            metadata={"mcp_server": server, "mcp_event": "reconnect", "attempt": attempt},
        ))
