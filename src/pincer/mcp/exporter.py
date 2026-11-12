"""
PincerToolExporter — maps Pincer ToolRegistry entries to FastMCP tool registrations.

Only tools in the `expose_tools` whitelist are exported.
`pincer_ask_user` is always registered regardless of the whitelist.
`calendar_today` is an MCP-only alias (see `pincer.mcp.calendar_alias`).
Results never carry the model's calendar edit hint (it names a tool MCP clients lack):
every call passes ``MCP_TOOL_CONTEXT``, which leaves it off at the source.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pincer.mcp.calendar_alias import CALENDAR_TODAY, DESCRIPTION, MCP_TOOL_CONTEXT, CalendarTodayAlias
from pincer.mcp.config import missing_export_log_level

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.server.fastmcp import FastMCP

    from pincer.mcp.security import MCPSecurityGate
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger("pincer.mcp.exporter")

# Default approval-required tools (destructive or sensitive)
_APPROVAL_REQUIRED = frozenset(
    {
        "email_send",
        "shell_exec",
        "python_exec",
        "file_write",
        "voice_call",
        "pincer_email_send",
        "pincer_shell_exec",
        "pincer_python_exec",
        "pincer_file_write",
        "pincer_voice_call",
    }
)

# Canonical prefix added to Pincer tools when exposed via MCP server
_TOOL_PREFIX = "pincer_"


class PincerToolExporter:
    """
    Registers a filtered subset of Pincer tools on a FastMCP server instance.

    Each exported tool is wrapped with:
    - Optional user-approval gate (for destructive/sensitive tools)
    - Audit logging (start / success / error)
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        expose_tools: list[str],
        approval_callback: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None,
        ask_user_callback: Callable[[str], Awaitable[str]] | None = None,
        security_gate: MCPSecurityGate | None = None,
    ) -> None:
        self._registry = tool_registry
        self._expose_tools = expose_tools
        self._approval_callback = approval_callback
        self._ask_user_callback = ask_user_callback
        self._security_gate = security_gate
        self._registered: list[str] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def export_to(self, fmcp: FastMCP) -> int:
        """
        Register exposed Pincer tools on *fmcp*.

        Returns the number of tools successfully registered.
        """
        self._registered.clear()
        registered_tools = self._registry.list_tools()

        for pincer_name in self._expose_tools:
            if pincer_name == CALENDAR_TODAY and pincer_name not in registered_tools:
                self._register_calendar_today(fmcp)
                continue
            if pincer_name not in registered_tools:
                logger.log(
                    missing_export_log_level(pincer_name),
                    "MCP export: tool '%s' not in registry — skipping",
                    pincer_name,
                )
                continue
            mcp_name = pincer_name if pincer_name.startswith(_TOOL_PREFIX) else f"{_TOOL_PREFIX}{pincer_name}"
            needs_approval = (
                pincer_name in _APPROVAL_REQUIRED
                or mcp_name in _APPROVAL_REQUIRED
                # Declared intent, not the local prompting policy: an external
                # MCP client crosses a trust boundary and must not inherit it.
                or self._registry.declares_approval(pincer_name)
            )
            self._wrap_tool(fmcp, pincer_name, mcp_name, needs_approval)
            self._registered.append(mcp_name)

        # pincer_ask_user is always exposed
        self._register_ask_user(fmcp)

        logger.info(
            "MCP server: exported %d tool(s) + pincer_ask_user",
            len(self._registered),
        )
        return len(self._registered)

    @property
    def registered_names(self) -> list[str]:
        """MCP names of currently registered tools (including pincer_ask_user)."""
        return list(self._registered) + ["pincer_ask_user"]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _wrap_tool(
        self,
        fmcp: FastMCP,
        pincer_name: str,
        mcp_name: str,
        needs_approval: bool,
    ) -> None:
        """Create an async handler for *pincer_name* and register it on *fmcp*."""
        registry = self._registry
        approval_cb = self._approval_callback

        async def _call(kwargs: dict[str, Any]) -> str:
            if needs_approval and approval_cb:
                try:
                    shown = await registry.approval_display_args(pincer_name, kwargs)
                    approved = await approval_cb(pincer_name, shown)
                except Exception:
                    logger.exception("Approval callback raised for '%s'", pincer_name)
                    approved = False
                if not approved:
                    return f"[Denied] User declined to execute {pincer_name}"
            return await registry.execute(pincer_name, kwargs, context=dict(MCP_TOOL_CONTEXT))

        async def _handler(**kwargs: Any) -> str:
            return await self._guarded(pincer_name, mcp_name, lambda: _call(kwargs))

        _handler.__name__ = mcp_name
        _handler.__doc__ = (
            self._registry._tools[pincer_name].description  # noqa: SLF001
            if pincer_name in self._registry._tools  # noqa: SLF001
            else mcp_name
        )
        fmcp.add_tool(_handler, name=mcp_name)

    def _register_calendar_today(self, fmcp: FastMCP) -> None:
        """The MCP-only ``pincer_calendar_today`` alias (read-only, no approval)."""
        alias = CalendarTodayAlias()
        mcp_name = f"{_TOOL_PREFIX}{CALENDAR_TODAY}"

        async def pincer_calendar_today(calendar_id: str = "primary") -> str:
            return await self._guarded(CALENDAR_TODAY, mcp_name, lambda: alias(calendar_id))

        pincer_calendar_today.__doc__ = DESCRIPTION
        fmcp.add_tool(pincer_calendar_today, name=mcp_name)
        self._registered.append(mcp_name)

    async def _guarded(self, pincer_name: str, mcp_name: str, call: Callable[[], Awaitable[str]]) -> str:
        """Rate limit (server = "pincer_server" for exported tools), then run *call*; errors become text."""
        security = self._security_gate
        if security and not security.check_rate_limit("pincer_server", pincer_name):
            return f"[RateLimited] Too many calls to {pincer_name} — slow down"
        try:
            return await call()
        except Exception as e:
            logger.exception("MCP server tool '%s' failed", mcp_name)
            return f"[Error] {pincer_name}: {type(e).__name__}: {e}"

    def _register_ask_user(self, fmcp: FastMCP) -> None:
        """Always register pincer_ask_user regardless of the expose_tools list."""
        ask_cb = self._ask_user_callback

        async def pincer_ask_user(question: str) -> str:
            """Ask the user a question through their active messaging channel.

            question: The question to ask the user
            """
            if ask_cb is None:
                return "[No active messaging channel — user unreachable]"
            try:
                return await ask_cb(question)
            except Exception as e:
                logger.exception("ask_user callback failed")
                return f"[Error contacting user: {e}]"

        fmcp.add_tool(pincer_ask_user, name="pincer_ask_user")
