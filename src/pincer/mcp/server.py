"""
PincerMCPServer — exposes Pincer built-in tools as an MCP server.

External MCP clients (Claude Desktop, Cursor, VS Code) connect to this server
and can call Pincer tools. Approval-gated tools prompt the user through their
active messaging channel before executing.

Binds to 127.0.0.1 by default (localhost-only for security).
Server is disabled by default; set [mcp.server] enabled = true in pincer.toml.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.mcp.exporter import PincerToolExporter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.server.fastmcp import FastMCP

    from pincer.mcp.auth import MCPAuthProvider
    from pincer.mcp.config import MCPServerExportConfig
    from pincer.mcp.prompts import PromptRegistry
    from pincer.mcp.resources import ResourceRegistry
    from pincer.mcp.sampling import SamplingHandler
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger("pincer.mcp.server")


class PincerMCPServer:
    """
    Manages the lifecycle of Pincer's outbound MCP server.

    Usage::

        server = PincerMCPServer(
            config=export_cfg,
            tool_registry=tool_registry,
            approval_callback=approval_fn,
            ask_user_callback=ask_user_fn,
        )
        await server.start()
        # ... agent runs ...
        await server.stop()
    """

    def __init__(
        self,
        config: MCPServerExportConfig,
        tool_registry: ToolRegistry,
        approval_callback: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None,
        ask_user_callback: Callable[[str], Awaitable[str]] | None = None,
        audit_logger: Any | None = None,
        auth_provider: MCPAuthProvider | None = None,
        resource_registry: ResourceRegistry | None = None,
        prompt_registry: PromptRegistry | None = None,
        sampling_handler: SamplingHandler | None = None,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._approval_callback = approval_callback
        self._ask_user_callback = ask_user_callback
        self._audit_logger = audit_logger
        self._auth_provider = auth_provider
        self._resource_registry = resource_registry
        self._prompt_registry = prompt_registry
        self._sampling_handler = sampling_handler

        self._fmcp: FastMCP | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._exposed_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize FastMCP, register tools, start serving in a background task."""
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:
            raise RuntimeError(
                "mcp package required to run MCP server: "
                "uv pip install 'pincer-agent[mcp]'"
            ) from e

        self._fmcp = FastMCP(
            "Pincer Agent",
            host=self._config.host,
            port=self._config.port,
            streamable_http_path=self._config.path,
            stateless_http=True,
        )

        exporter = PincerToolExporter(
            tool_registry=self._tool_registry,
            expose_tools=self._config.expose_tools,
            approval_callback=self._approval_callback,
            ask_user_callback=self._ask_user_callback,
        )
        self._exposed_count = exporter.export_to(self._fmcp)

        # Register resources if provided
        if self._resource_registry is not None:
            with contextlib.suppress(Exception):
                self._resource_registry.export_to(self._fmcp)

        # Register prompts if provided
        if self._prompt_registry is not None:
            with contextlib.suppress(Exception):
                self._prompt_registry.export_to(self._fmcp)

        # Wire sampling handler if provided
        if self._sampling_handler is not None:
            with contextlib.suppress(Exception):
                self._sampling_handler.register_on(self._fmcp)

        # Wire auth middleware if configured (requires mcp/starlette extras)
        if self._auth_provider:
            with contextlib.suppress(Exception):
                from pincer.mcp.auth import MCPAuthMiddleware
                starlette_app = self._fmcp.streamable_http_app()
                starlette_app.add_middleware(MCPAuthMiddleware, auth_provider=self._auth_provider)

        self._serve_task = asyncio.create_task(
            self._fmcp.run_streamable_http_async(),
            name="pincer-mcp-server",
        )

        logger.info(
            "MCP server started at %s (%d tool(s) exposed)",
            self.endpoint,
            self._exposed_count,
        )

        if self._audit_logger:
            with contextlib.suppress(Exception):
                await self._audit_logger.log_connect("mcp_server_export", success=True)

    async def stop(self) -> None:
        """Cancel the background server task and clean up."""
        if self._serve_task:
            self._serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
            self._serve_task = None

        if self._audit_logger:
            with contextlib.suppress(Exception):
                await self._audit_logger.log_disconnect("mcp_server_export")

        logger.info("MCP server stopped")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """True if the background server task is alive."""
        return (
            self._serve_task is not None
            and not self._serve_task.done()
        )

    @property
    def endpoint(self) -> str:
        """Full URL of the MCP endpoint."""
        return f"http://{self._config.host}:{self._config.port}{self._config.path}"

    @property
    def exposed_tool_count(self) -> int:
        """Number of tools registered (excluding pincer_ask_user)."""
        return self._exposed_count
