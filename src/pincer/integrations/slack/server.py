"""Standalone Slack FastMCP server.

Can run in two modes:
  - stdio  (for Claude Desktop / MCP clients that spawn a subprocess)
  - http   (for network-accessible MCP endpoints, e.g. pincer mcp list)

Usage::

    python -m pincer.integrations.slack.server stdio
    python -m pincer.integrations.slack.server http --port 18910
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 18910
_DEFAULT_PATH = "/slack/mcp"


def _build_fastmcp() -> object:
    """Build a FastMCP instance with all 71 Slack tools registered."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'pincer-agent[mcp]'"
        ) from exc

    from pincer.integrations.slack import get_slack_client, register_all_tools
    from pincer.tools.registry import ToolRegistry

    client = get_slack_client()
    if client is None:
        raise RuntimeError(
            "Slack tokens not configured. Run: pincer setup-slack"
        )

    registry = ToolRegistry()
    count = register_all_tools(registry, client)
    logger.info("Slack MCP server: %d tools registered", count)

    fmcp = FastMCP("Pincer Slack", stateless_http=True)

    for name, tool_def in registry._tools.items():
        _register_fmcp_tool(fmcp, name, tool_def)

    return fmcp


def _register_fmcp_tool(fmcp: object, name: str, tool_def: object) -> None:
    """Wrap a ToolRegistry entry as a FastMCP tool."""
    import functools

    handler = tool_def.handler  # type: ignore[attr-defined]
    description = tool_def.description  # type: ignore[attr-defined]

    @fmcp.tool(name=name, description=description)  # type: ignore[attr-defined,untyped-decorator]
    @functools.wraps(handler)
    async def _tool(**kwargs: object) -> str:
        try:
            result = await handler(**kwargs)
            return str(result)
        except Exception as exc:
            return f"Error: {exc}"


class SlackMCPServer:
    """Lifecycle manager for the standalone Slack MCP HTTP server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = _DEFAULT_PORT,
        path: str = _DEFAULT_PATH,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._task: object = None

    @property
    def endpoint(self) -> str:
        return f"http://{self._host}:{self._port}{self._path}"

    async def start(self) -> None:
        """Build and start the MCP HTTP server."""
        import asyncio

        fmcp = _build_fastmcp()
        app = fmcp.get_asgi_app()  # type: ignore[attr-defined]

        import uvicorn

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve())
        logger.info("Slack MCP server started at %s", self.endpoint)

    async def stop(self) -> None:
        if self._task:
            import asyncio
            import contextlib

            task = self._task  # type: ignore[assignment]
            task.cancel()  # type: ignore[attr-defined]
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)  # type: ignore[arg-type]
            self._task = None


def main() -> None:
    """Entry point for running the server from the command line."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Pincer Slack MCP server")
    parser.add_argument("mode", choices=["stdio", "http"], default="stdio", nargs="?")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.mode == "stdio":
        import asyncio

        fmcp = _build_fastmcp()
        asyncio.run(fmcp.run_stdio_async())  # type: ignore[attr-defined]
    else:
        server = SlackMCPServer(host=args.host, port=args.port)

        async def _serve() -> None:
            await server.start()
            print(f"Slack MCP server running at {server.endpoint}")
            try:
                import asyncio

                await asyncio.sleep(float("inf"))
            except KeyboardInterrupt:
                pass

        asyncio.run(_serve())


if __name__ == "__main__":
    main()
