"""
Google Workspace FastMCP server.

Registers all 85 Google Workspace tools on a FastMCP instance.
Can run in two modes:

  - stdio (for Claude Desktop / MCP clients that spawn a subprocess):
        python -m pincer.integrations.google.server stdio

  - HTTP (for network-accessible MCP endpoints):
        python -m pincer.integrations.google.server http --port 18900

The server can also be embedded in a larger application::

    from pincer.integrations.google.server import GoogleWorkspaceMCPServer
    server = GoogleWorkspaceMCPServer(auth)
    await server.start()
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.google.auth import GoogleAuth

logger = logging.getLogger(__name__)


def _build_fastmcp(auth: "GoogleAuth") -> Any:
    """Build a FastMCP instance with all 85 Google Workspace tools registered."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "mcp package required: uv pip install 'pincer-agent[mcp]'"
        ) from exc

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

    factory = GoogleServiceFactory(auth)
    registry = ToolRegistry()

    from pincer.integrations.google import register_all_tools
    count = register_all_tools(registry, factory)

    fmcp = FastMCP("Pincer Google Workspace", stateless_http=True)

    # Wrap each registered tool as a FastMCP tool.
    for name, tool_def in registry._tools.items():  # type: ignore[attr-defined]
        handler = tool_def.handler
        description = tool_def.description

        # Build a FastMCP-compatible wrapper
        def _make_tool(tool_name: str, tool_handler: Any, tool_desc: str) -> None:
            @fmcp.tool(name=tool_name, description=tool_desc)
            async def _tool(**kwargs: Any) -> str:
                try:
                    result = await tool_handler(**kwargs)
                    return str(result)
                except Exception as exc:
                    logger.error("Tool %s failed: %s", tool_name, exc)
                    return f"Error: {exc}"

        _make_tool(name, handler, description)

    logger.info("Google Workspace MCP server: %d tools registered", count)
    return fmcp


class GoogleWorkspaceMCPServer:
    """Manages the lifecycle of a standalone Google Workspace MCP server."""

    def __init__(
        self,
        auth: "GoogleAuth",
        host: str = "127.0.0.1",
        port: int = 18900,
        path: str = "/mcp",
    ) -> None:
        self._auth = auth
        self._host = host
        self._port = port
        self._path = path
        self._fmcp: Any = None
        self._serve_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Build and start the HTTP server in a background task."""
        self._fmcp = _build_fastmcp(self._auth)
        self._fmcp.settings.host = self._host
        self._fmcp.settings.port = self._port
        self._fmcp.settings.streamable_http_path = self._path

        self._serve_task = asyncio.create_task(
            self._fmcp.run_streamable_http_async(),
            name="pincer-google-mcp-server",
        )
        logger.info("Google Workspace MCP server started at %s", self.endpoint)

    async def stop(self) -> None:
        if self._serve_task:
            self._serve_task.cancel()
            import contextlib
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
            self._serve_task = None
        logger.info("Google Workspace MCP server stopped")

    @property
    def running(self) -> bool:
        return self._serve_task is not None and not self._serve_task.done()

    @property
    def endpoint(self) -> str:
        return f"http://{self._host}:{self._port}{self._path}"


def main() -> None:
    """CLI entry point: ``python -m pincer.integrations.google.server [stdio|http]``."""
    import argparse
    from pathlib import Path

    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.auth import GoogleAuth

    parser = argparse.ArgumentParser(description="Google Workspace MCP Server")
    parser.add_argument("mode", choices=["stdio", "http"], default="stdio", nargs="?")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18900)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args()

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()
    auth = GoogleAuth(
        credentials_path=str(oauth_dir / "google_credentials.json"),
        token_path=str(oauth_dir / "google_workspace_token.json"),
    )

    fmcp = _build_fastmcp(auth)

    if args.mode == "stdio":
        asyncio.run(fmcp.run_stdio_async())
    else:
        fmcp.settings.host = args.host
        fmcp.settings.port = args.port
        fmcp.settings.streamable_http_path = args.path
        asyncio.run(fmcp.run_streamable_http_async())


if __name__ == "__main__":
    main()
