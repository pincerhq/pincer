"""
MCPClientManager — orchestrates all MCP server connections.

Responsibilities:
- Parallel startup with graceful degradation (failed servers don't block boot)
- Background health checks with automatic reconnection
- Tool change notification handling (tools/list_changed)
- Clean shutdown with tool deregistration
- Status reporting for `pincer mcp list` and `pincer doctor`
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio

from pincer.mcp.audit import MCPAuditLogger
from pincer.mcp.bridge import MCPToolBridge
from pincer.mcp.client import MCPClientSession

if TYPE_CHECKING:
    from pincer.mcp.config import MCPConfig
    from pincer.mcp.security import MCPSecurityGate
    from pincer.security.audit import AuditLogger
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger("pincer.mcp")

_HEALTH_INTERVAL_SECONDS = 60


class MCPClientManager:
    """
    Manages the lifecycle of all configured MCP client sessions.

    Usage:
        manager = MCPClientManager(config, tool_registry, audit_logger)
        results = await manager.start()   # {server_name: bool}
        # ... agent runs ...
        await manager.stop()
    """

    def __init__(
        self,
        config: MCPConfig,
        tool_registry: ToolRegistry,
        audit_logger: AuditLogger | None = None,
        security_gate: MCPSecurityGate | None = None,
    ) -> None:
        self.config = config
        self.tool_registry = tool_registry
        self._audit = MCPAuditLogger(audit_logger, security_gate)
        self._security_gate = security_gate
        self._sessions: dict[str, MCPClientSession] = {}
        self._bridges: dict[str, MCPToolBridge] = {}
        self._task_group: anyio.abc.TaskGroup | None = None
        self._task_group_ctx: Any = None
        self._disabled: set[str] = set()  # servers that exhausted retries

    async def start(self) -> dict[str, bool]:
        """
        Connect to all enabled MCP servers in parallel.

        Returns dict of {server_name: connection_success}.
        Failed servers are skipped with a warning — they don't block startup.
        """
        if not self.config.enabled:
            return {}

        enabled_servers = [s for s in self.config.servers if s.enabled]
        if not enabled_servers:
            return {}

        results: dict[str, bool] = {}

        async def _connect_one(srv_config: Any) -> None:

            session = MCPClientSession(srv_config)
            try:
                # Client handles startup_timeout + timeout internally
                await session.connect()
            except Exception as e:
                logger.warning("MCP '%s' failed to connect: %s", srv_config.name, e)
                await self._audit.log_connect(srv_config.name, success=False, error=str(e))
                results[srv_config.name] = False
                return

            self._sessions[srv_config.name] = session
            bridge = MCPToolBridge(
                session=session,
                tool_registry=self.tool_registry,
                audit_logger=self._audit,
                prefix=self.config.tool_prefix,
                security_gate=self._security_gate,
            )
            bridge.register_tools()
            self._bridges[srv_config.name] = bridge
            await self._audit.log_connect(srv_config.name, success=True)
            results[srv_config.name] = True

        async with anyio.create_task_group() as tg:
            for s in enabled_servers:
                tg.start_soon(_connect_one, s)

        # Start background health check loop via persistent task group
        tg_ctx = anyio.create_task_group()
        self._task_group = await tg_ctx.__aenter__()
        self._task_group_ctx = tg_ctx
        self._task_group.start_soon(self._health_loop)

        return results

    async def stop(self) -> None:
        """Disconnect all MCP servers and deregister tools."""
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
            await self._task_group_ctx.__aexit__(None, None, None)
            self._task_group = None
            self._task_group_ctx = None

        for _name, bridge in list(self._bridges.items()):
            bridge.deregister_tools()

        for name, session in list(self._sessions.items()):
            try:
                await session.disconnect()
                await self._audit.log_disconnect(name)
            except Exception as e:
                await self._audit.log_disconnect(name, error=str(e))

        self._sessions.clear()
        self._bridges.clear()

    async def get_session(self, server_name: str) -> MCPClientSession:
        """Get a connected session by name. Raises ValueError if not found."""
        session = self._sessions.get(server_name)
        if not session:
            raise ValueError(f"MCP server '{server_name}' not connected. Available: {list(self._sessions.keys())}")
        return session

    async def list_servers(self) -> list[dict[str, Any]]:
        """Return status info for all configured servers (for `pincer mcp list`)."""
        statuses: list[dict[str, Any]] = []
        for srv in self.config.servers:
            session = self._sessions.get(srv.name)
            statuses.append(
                {
                    "name": srv.name,
                    "transport": srv.transport.value,
                    "enabled": srv.enabled,
                    "connected": session.connected if session else False,
                    "tool_count": len(session.tools) if session else 0,
                    "disabled": srv.name in self._disabled,
                    "sandbox": srv.sandbox,
                    "approval_required": srv.approval_required,
                }
            )
        return statuses

    def total_tool_count(self) -> int:
        """Total MCP tools currently registered."""
        return sum(len(s.tools) for s in self._sessions.values() if s.connected)

    # ── Background tasks ──────────────────────────────────

    async def _health_loop(self) -> None:
        """Background task: periodic health checks + reconnection."""
        while True:
            try:
                await anyio.sleep(_HEALTH_INTERVAL_SECONDS)
                await self._run_health_checks()
            except Exception:
                logger.debug("MCP health loop error", exc_info=True)

    async def _run_health_checks(self) -> None:
        for name, session in list(self._sessions.items()):
            if name in self._disabled:
                continue
            healthy = await session.health_check()
            await self._audit.log_health_check(name, healthy=healthy)
            if not healthy:
                logger.warning("MCP '%s' health check failed — attempting reconnect", name)
                await self._reconnect(name)

    async def _handle_tools_changed(self, server_name: str) -> None:
        """Called when an MCP server notifies that its tools list has changed."""
        session = self._sessions.get(server_name)
        if not session:
            logger.warning("MCP tools_changed for unknown server: %s", server_name)
            return
        await session.refresh_tools()
        bridge = self._bridges.get(server_name)
        if bridge:
            count = bridge.update_tools()
            logger.info("MCP '%s': tools updated (%d registered)", server_name, count)

    async def _reconnect(self, name: str) -> None:
        """Attempt to reconnect a disconnected server."""
        session = self._sessions.get(name)
        if not session:
            return

        srv_config = session.config
        for attempt in range(1, srv_config.max_retries + 1):
            try:
                new_session = MCPClientSession(srv_config)
                await new_session.connect()

                # Deregister old tools, register new ones
                if name in self._bridges:
                    self._bridges[name].deregister_tools()

                self._sessions[name] = new_session
                bridge = MCPToolBridge(
                    session=new_session,
                    tool_registry=self.tool_registry,
                    audit_logger=self._audit,
                    prefix=self.config.tool_prefix,
                    security_gate=self._security_gate,
                )
                bridge.register_tools()
                self._bridges[name] = bridge

                await self._audit.log_reconnect(name, attempt=attempt, success=True)
                logger.info("MCP '%s' reconnected (attempt %d)", name, attempt)
                return

            except Exception as e:
                await self._audit.log_reconnect(name, attempt=attempt, success=False, error=str(e))
                logger.warning("MCP '%s' reconnect attempt %d failed: %s", name, attempt, e)
                if attempt < srv_config.max_retries:
                    await anyio.sleep(2**attempt)  # exponential backoff

        logger.error(
            "MCP '%s' exhausted %d reconnect attempts — disabling until restart",
            name,
            srv_config.max_retries,
        )
        self._disabled.add(name)
        if name in self._bridges:
            self._bridges[name].deregister_tools()
