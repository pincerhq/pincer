"""
MCPServiceCore — agent-independent MCP coordination layer.

MCPServiceCore is the heart of Pincer's MCP support. It owns:
- Tool registration for server-side exposure (tools MCP clients can call)
- Resource registration and serving
- Prompt registration and serving
- Sampling request handling
- Security gate and audit logging
- Client-side MCP connections (consuming external MCP servers)

It does NOT own:
- Messaging channels (Telegram, WhatsApp, etc.)
- Agent loop / ReAct cycle
- Soul / system prompt
- Channel-specific approval routing

Both thin shells (EmbeddedMCPShell and StandaloneMCPShell) build on this core.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pincer.mcp.approval import ApprovalDecision, ApprovalRequest, classify_risk
from pincer.mcp.prompts import PromptRegistry
from pincer.mcp.resources import ResourceRegistry
from pincer.mcp.sampling import SamplingHandler

# Lazy imports for optional mcp package — used in start_clients() / start_server()
try:
    from pincer.mcp.audit import MCPAuditLogger
    from pincer.mcp.manager import MCPClientManager
    from pincer.mcp.security import MCPSecurityGate
    from pincer.mcp.server import PincerMCPServer
except ImportError:
    MCPAuditLogger = None  # type: ignore[assignment,misc]
    MCPClientManager = None  # type: ignore[assignment,misc]
    MCPSecurityGate = None  # type: ignore[assignment,misc]
    PincerMCPServer = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pincer.mcp.approval import ApprovalBackend
    from pincer.mcp.config import MCPConfig
    from pincer.mcp.sampling import LLMBackend
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger("pincer.mcp.core")


# ── Server-side tool descriptor ────────────────────────────────────────────────


@dataclass
class ServerToolDef:
    """Tool registered via register_tool() for MCP server-side exposure."""

    name: str
    handler: Callable[..., Awaitable[str]]
    schema: dict[str, Any]
    approval_required: bool
    description: str = ""


# ── Core class ─────────────────────────────────────────────────────────────────


class MCPServiceCore:
    """
    Agent-independent MCP coordination layer.

    Usage (typical)::

        core = MCPServiceCore(
            config=mcp_cfg,
            approval_backend=PolicyApprovalBackend({"read": "approve", "default": "deny"}),
        )
        core.register_tool("pincer_web_search", handler, schema, approval_required=False)
        core.register_resource("pincer://memory/recent", memory_handler, "Recent memory")
        client_results = await core.start_clients()   # consume external MCP servers
        await core.start_server()                     # expose tools/resources/prompts
        ...
        await core.stop_server()
        await core.stop_clients()
    """

    def __init__(
        self,
        config: MCPConfig,
        approval_backend: ApprovalBackend,
        client_tool_registry: ToolRegistry | None = None,
        llm_backend: LLMBackend | None = None,
        audit_log: Any | None = None,
    ) -> None:
        """
        Args:
            config:               MCP configuration.
            approval_backend:     Backend for approval decisions on server-side tool calls.
            client_tool_registry: ToolRegistry where MCP client tools are registered.
                                  If None, a fresh ToolRegistry is created (standalone mode).
                                  In embedded mode, pass the agent's registry so tools
                                  are immediately visible to the agent loop.
            llm_backend:          Optional LLM backend for sampling support.
            audit_log:            Optional Pincer AuditLogger for event logging.
        """
        self.config = config
        self._approval = approval_backend
        self._llm_backend = llm_backend
        self._audit_log = audit_log

        # Registry for tools imported FROM external MCP servers (client side)
        if client_tool_registry is None:
            from pincer.tools.registry import ToolRegistry as _TR
            self._client_tool_registry = _TR()
        else:
            self._client_tool_registry = client_tool_registry

        # Server-side tools registered via register_tool()
        self._server_tools: dict[str, ServerToolDef] = {}

        # Resource and prompt registries (server-side)
        self.resource_registry = ResourceRegistry()
        self.prompt_registry = PromptRegistry()

        # Sampling handler
        sampling_cfg = getattr(getattr(config, "server", None), "sampling", None)
        if sampling_cfg and getattr(sampling_cfg, "enabled", False):
            self.sampling_handler = SamplingHandler(
                llm_backend=llm_backend,
                enabled=True,
                budget_per_day=getattr(sampling_cfg, "budget_per_day", 5.0),
                max_tokens=getattr(sampling_cfg, "max_tokens", 1024),
                default_model=getattr(sampling_cfg, "model", None),
            )
        else:
            self.sampling_handler = SamplingHandler(enabled=False)

        # Runtime state
        self._client_manager: Any | None = None   # MCPClientManager
        self._server: Any | None = None            # PincerMCPServer

    # ── Server-side: tool/resource/prompt registration ─────────────────────────

    def register_tool(
        self,
        name: str,
        handler: Callable[..., Awaitable[str]],
        schema: dict[str, Any],
        approval_required: bool = True,
        description: str = "",
    ) -> None:
        """Register a tool for MCP server-side exposure."""
        self._server_tools[name] = ServerToolDef(
            name=name,
            handler=handler,
            schema=schema,
            approval_required=approval_required,
            description=description,
        )
        logger.debug("MCPServiceCore: registered server tool '%s'", name)

    def register_resource(
        self,
        uri: str,
        handler: Callable[[], Awaitable[str]],
        description: str,
        mime_type: str = "text/plain",
    ) -> None:
        """Register a resource for MCP server-side exposure."""
        self.resource_registry.register(uri=uri, handler=handler, description=description, mime_type=mime_type)

    def register_prompt(
        self,
        name: str,
        handler: Callable[..., Awaitable[list[dict[str, Any]]]],
        description: str,
        arguments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Register a prompt template for MCP server-side exposure."""
        self.prompt_registry.register(name=name, handler=handler, description=description, arguments=arguments)

    # ── Client-side: consume external MCP servers ──────────────────────────────

    async def start_clients(self) -> dict[str, bool]:
        """
        Connect to all configured external MCP servers.

        Returns dict of {server_name: success}.
        Failed servers don't block startup (graceful degradation).
        """
        if not self.config.enabled or not self.config.servers:
            return {}

        if MCPClientManager is None:
            logger.warning("MCP client not available (mcp package not installed)")
            return {}

        security_gate = MCPSecurityGate()
        MCPAuditLogger(self._audit_log, security_gate)

        self._client_manager = MCPClientManager(
            config=self.config,
            tool_registry=self._client_tool_registry,
            audit_logger=self._audit_log,
            security_gate=security_gate,
        )
        return await self._client_manager.start()

    async def stop_clients(self) -> None:
        """Disconnect all external MCP servers and deregister their tools."""
        if self._client_manager is not None:
            await self._client_manager.stop()
            self._client_manager = None

    # ── Server-side: start/stop MCP server ────────────────────────────────────

    async def start_server(self) -> None:
        """
        Start the MCP server — exposes registered tools, resources, and prompts.

        No-op if `config.server.enabled` is False.
        """
        server_cfg = getattr(self.config, "server", None)
        if server_cfg is None or not getattr(server_cfg, "enabled", False):
            logger.debug("MCPServiceCore: MCP server not enabled — skipping start_server()")
            return

        if PincerMCPServer is None:
            raise RuntimeError(
                "mcp package required for MCP server: uv pip install 'pincer-agent[mcp]'"
            )

        # Build a ToolRegistry from server-side tool registrations
        from pincer.tools.registry import ToolRegistry
        server_registry = ToolRegistry()
        for tool_def in self._server_tools.values():
            server_registry.register(
                name=tool_def.name,
                description=tool_def.description or tool_def.name,
                handler=tool_def.handler,
                parameters=tool_def.schema,
                require_approval=tool_def.approval_required,
            )

        # Build approval callback that delegates to the ApprovalBackend
        approval = self._approval

        async def _approval_callback(tool_name: str, args: dict[str, Any]) -> bool:
            risk = classify_risk(tool_name)
            req = ApprovalRequest(
                tool_name=tool_name,
                arguments=args,
                source="mcp_client:unknown",
                risk_level=risk,
                description=f"Execute tool '{tool_name}'",
            )
            decision = await approval.request_approval(req)
            return decision == ApprovalDecision.APPROVED

        self._server = PincerMCPServer(
            config=server_cfg,
            tool_registry=server_registry,
            approval_callback=_approval_callback,
            ask_user_callback=None,
            audit_logger=self._audit_log,
            resource_registry=self.resource_registry,
            prompt_registry=self.prompt_registry,
            sampling_handler=self.sampling_handler,
        )
        await self._server.start()

    async def stop_server(self) -> None:
        """Stop the MCP server."""
        if self._server is not None:
            await self._server.stop()
            self._server = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def server_running(self) -> bool:
        """True if the MCP server is currently running."""
        return self._server is not None and getattr(self._server, "running", False)

    @property
    def server_endpoint(self) -> str | None:
        """MCP server endpoint URL, or None if not running."""
        if self._server is not None:
            return getattr(self._server, "endpoint", None)
        return None

    @property
    def client_tool_count(self) -> int:
        """Number of tools registered from external MCP servers."""
        if self._client_manager is not None:
            return self._client_manager.total_tool_count()
        return 0

    @property
    def server_tool_count(self) -> int:
        """Number of tools registered for server-side exposure."""
        return len(self._server_tools)

    async def list_connected_clients(self) -> list[dict[str, Any]]:
        """Return status info for all configured client servers."""
        if self._client_manager is not None:
            return await self._client_manager.list_servers()
        return []
