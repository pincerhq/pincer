"""
EmbeddedMCPShell — MCP integration when running inside the full Pincer agent.

This thin shell wires MCPServiceCore to the running agent:
- Uses ChannelApprovalBackend (approval via Telegram/WhatsApp/etc.)
- Passes agent's ToolRegistry to MCPClientManager (so MCP client tools
  appear alongside built-in tools in the agent's context)
- Registers agent's built-in tools for server-side export
- Registers live data resources (memory, calendar, email summaries)
- Registers ask_user as a special server-side tool
- Handles agent.mcp_shell reference for status/health access

Usage (inside cli.py / _run_agent)::

    shell = EmbeddedMCPShell(agent=agent, mcp_config=mcp_cfg)
    await shell.start()
    # ... agent runs ...
    await shell.stop()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.core.agent import Agent
    from pincer.mcp.config import MCPConfig

logger = logging.getLogger("pincer.mcp.embedded")


class EmbeddedMCPShell:
    """
    Thin shell that wires MCPServiceCore to the running Pincer agent.

    Approval decisions route to the user's active messaging channel.
    If no channel is active when an approval arrives, the request is denied.

    Args:
        agent:      The running Pincer agent instance.
        mcp_config: MCP configuration (loaded via load_mcp_config()).
    """

    def __init__(self, agent: Agent, mcp_config: MCPConfig) -> None:
        self._agent = agent
        self._mcp_config = mcp_config
        self._core: Any | None = None  # MCPServiceCore — set in start()

    async def start(self) -> dict[str, bool]:
        """
        Start MCP client connections and (optionally) the MCP server.

        Returns dict of {server_name: connection_success} for client connections.
        """
        from pincer.mcp.approval import ChannelApprovalBackend
        from pincer.mcp.core import MCPServiceCore

        agent = self._agent

        # Build approval backend — late-binds to agent's ask_user callback
        def _get_ask_cb():
            return getattr(agent, "_ask_user_callback", None)

        approval_backend = ChannelApprovalBackend(get_ask_callback=_get_ask_cb)

        # Build core — share agent's tool registry for client-side tool imports
        self._core = MCPServiceCore(
            config=self._mcp_config,
            approval_backend=approval_backend,
            client_tool_registry=agent.tool_registry,
            audit_log=getattr(agent, "_audit_log", None),
        )

        # Register agent's built-in tools for server-side export
        self._register_agent_tools_for_export()

        # Register live data resources
        self._register_live_resources()

        # Register ask_user as a special server-side tool
        self._register_ask_user_tool()

        # Connect to external MCP servers (client side)
        client_results = await self._core.start_clients()

        if client_results:
            connected = sum(1 for ok in client_results.values() if ok)
            logger.info("EmbeddedMCPShell: %d/%d MCP servers connected", connected, len(client_results))

        # Start MCP server if enabled
        server_cfg = getattr(self._mcp_config, "server", None)
        if server_cfg and getattr(server_cfg, "enabled", False):
            await self._core.start_server()
            logger.info(
                "EmbeddedMCPShell: MCP server started at %s",
                self._core.server_endpoint,
            )

        # Attach shell to agent for health/status access
        agent.mcp_shell = self

        return client_results

    async def stop(self) -> None:
        """Stop MCP server and all client connections."""
        if self._core is not None:
            await self._core.stop_server()
            await self._core.stop_clients()
            self._core = None

    # ── Private helpers ────────────────────────────────────────────────────────

    def _register_agent_tools_for_export(self) -> None:
        """Register agent built-in tools in the server-side registry for MCP export."""
        if self._core is None:
            return

        server_cfg = getattr(self._mcp_config, "server", None)
        if server_cfg is None:
            return

        expose_tools: list[str] = getattr(server_cfg, "expose_tools", [])
        if not expose_tools:
            return

        agent = self._agent
        registered_tools = agent.tool_registry.list_tools()

        for tool_name in expose_tools:
            if tool_name not in registered_tools:
                logger.debug("EmbeddedMCPShell: tool '%s' not in registry — skipping export", tool_name)
                continue

            tool_def = agent.tool_registry._tools.get(tool_name)  # noqa: SLF001
            if tool_def is None:
                continue

            mcp_name = (
                tool_name if tool_name.startswith("pincer_")
                else f"pincer_{tool_name}"
            )
            approval = agent.tool_registry.requires_approval(tool_name)

            self._core.register_tool(
                name=mcp_name,
                handler=tool_def.handler,
                schema=tool_def.parameters,
                approval_required=approval,
                description=tool_def.description,
            )
            logger.debug("EmbeddedMCPShell: exported tool '%s' → '%s'", tool_name, mcp_name)

    def _register_live_resources(self) -> None:
        """Register live data resources backed by agent state."""
        if self._core is None:
            return

        agent = self._agent

        async def _memory_resource() -> str:
            """Return recent conversation memory entries."""
            try:
                memory = getattr(agent, "_memory", None)
                if memory is None:
                    return "(Memory store not available)"
                entries = await memory.get_recent(limit=10)
                if not entries:
                    return "(No recent memory entries)"
                return "\n".join(str(e) for e in entries)
            except Exception as e:
                return f"(Error reading memory: {e})"

        self._core.register_resource(
            uri="pincer://memory/recent",
            handler=_memory_resource,
            description="Recent conversation memory entries",
            mime_type="text/plain",
        )

        async def _agent_status_resource() -> str:
            """Return current agent status and configuration."""
            try:
                lines = [
                    f"Agent: Pincer",
                    f"Tools: {len(agent.tool_registry.list_tools())}",
                    f"MCP clients connected: {self._core.client_tool_count if self._core else 0}",
                ]
                return "\n".join(lines)
            except Exception as e:
                return f"(Error reading agent status: {e})"

        self._core.register_resource(
            uri="pincer://agent/status",
            handler=_agent_status_resource,
            description="Current agent status and configuration summary",
            mime_type="text/plain",
        )

    def _register_ask_user_tool(self) -> None:
        """Register pincer_ask_user as a server-side tool (always available)."""
        if self._core is None:
            return

        agent = self._agent

        async def _ask_user_handler(question: str) -> str:
            """Ask the user a question through their active messaging channel."""
            try:
                return await agent._ask_user_on_active_channel(question)  # noqa: SLF001
            except Exception as e:
                logger.exception("ask_user handler failed: %s", e)
                return f"[Error contacting user: {e}]"

        self._core.register_tool(
            name="pincer_ask_user",
            handler=_ask_user_handler,
            schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question to ask the user"}
                },
                "required": ["question"],
            },
            approval_required=False,
            description="Ask the user a question through their active messaging channel",
        )

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def core(self) -> Any | None:
        """The underlying MCPServiceCore instance (None if not started)."""
        return self._core

    @property
    def server_running(self) -> bool:
        return self._core is not None and self._core.server_running

    @property
    def server_endpoint(self) -> str | None:
        return self._core.server_endpoint if self._core else None

    @property
    def client_tool_count(self) -> int:
        return self._core.client_tool_count if self._core else 0
