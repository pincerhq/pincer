"""
Standalone Microsoft 365 MCP server.

Exposes Microsoft 365 tools (Outlook email, Calendar, OneDrive, To Do,
Contacts, OneNote) as an MCP server so any MCP host — Claude Desktop,
Cursor, or Pincer's own MCP client — can drive Microsoft Graph.

Transports::

    ms365-mcp-run --transport stdio                         # default
    ms365-mcp-run --transport http --host 127.0.0.1 --port 8000
        MCP endpoint → http://127.0.0.1:8000/mcp

Authentication: set ``MS365_CLIENT_ID`` (and optionally
``MS365_TENANT_ID``) then start the server.  If no cached token exists
the server runs the device code flow at boot — instructions are printed to
stderr so they are visible without corrupting the MCP stdio stream.
Token is cached at ``~/.pincer/ms365_token_cache.json`` for subsequent starts.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ms365.graph_client import GraphClient

from ms365.config import get_settings

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.value)
)
logger = logging.getLogger("ms365.ms365_server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
SERVICES = ("email", "calendar", "onedrive", "todo", "contacts", "onenote")


def _registrars() -> dict[str, Callable[..., int]]:
    """Map each service to the register function that emits its tools."""
    from ms365.tools_calendar import register_calendar_tools
    from ms365.tools_contacts import register_contacts_tools
    from ms365.tools_email import register_email_tools
    from ms365.tools_onedrive import register_onedrive_tools
    from ms365.tools_onenote import register_onenote_tools
    from ms365.tools_todo import register_todo_tools

    return {
        "email": register_email_tools,
        "calendar": register_calendar_tools,
        "onedrive": register_onedrive_tools,
        "todo": register_todo_tools,
        "contacts": register_contacts_tools,
        "onenote": register_onenote_tools,
    }


@dataclass
class _ToolSpec:
    """A collected MS365 tool: name, schema, handler, and approval flag."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    require_approval: bool


class _CollectingRegistry:
    """Drop-in stand-in for ``ToolRegistry`` that records registrations.

    Implements only the ``register`` signature the ``register_*_tools``
    functions call, so the existing tool definitions are reused unchanged.
    """

    def __init__(self) -> None:
        self.specs: list[_ToolSpec] = []

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[..., Awaitable[str]],
        parameters: dict[str, Any] | None = None,
        require_approval: bool = False,
    ) -> None:
        self.specs.append(
            _ToolSpec(
                name=name,
                description=description,
                parameters=parameters or {"type": "object", "properties": {}, "required": []},
                handler=handler,
                require_approval=require_approval,
            )
        )


def collect_tools(
    client: GraphClient,
    services: list[str] | None = None,
    read_only: bool = False,
) -> list[_ToolSpec]:
    """Collect tool specs for the requested services by reusing the registrars.

    Args:
        client: Authenticated Graph client injected into every handler.
        services: Subset of service names to expose; ``None`` exposes all.
        read_only: When True, drop tools that require approval (writes/deletes).
    """
    registrars = _registrars()
    registry = _CollectingRegistry()

    for svc in services or SERVICES:
        register_fn = registrars.get(svc)
        if register_fn is None:
            logger.warning("Unknown MS365 service '%s' — skipping", svc)
            continue
        register_fn(registry, client)

    if read_only:
        return [s for s in registry.specs if not s.require_approval]
    return registry.specs


def build_server(
    client: GraphClient,
    services: list[str] | None = None,
    read_only: bool = False,
) -> tuple[Any, list[_ToolSpec]]:
    """Build a low-level MCP ``Server`` exposing the collected MS365 tools.

    Returns the server and the list of specs it serves.
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server

    specs = collect_tools(client, services=services, read_only=read_only)
    by_name = {s.name: s for s in specs}

    server: Server = Server("pincer-ms365")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=s.name,
                description=s.description,
                inputSchema=s.parameters,
                annotations=types.ToolAnnotations(
                    readOnlyHint=not s.require_approval,
                    destructiveHint=s.require_approval,
                ),
            )
            for s in specs
        ]

    @server.call_tool()  # type: ignore[no-untyped-call,untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        spec = by_name.get(name)
        if spec is None:
            return [types.TextContent(type="text", text=f"[Error] Unknown tool: {name}")]
        try:
            result = await spec.handler(**(arguments or {}))
        except Exception as exc:
            logger.exception("MS365 tool '%s' failed", name)
            result = f"[Error] {name}: {type(exc).__name__}: {exc}"
        return [types.TextContent(type="text", text=result)]

    logger.debug("MS365 MCP server built with %d tool(s)", len(specs))
    return server, specs


# ── Transports ────────────────────────────────────────────────────────────────


async def run_stdio(server: Any) -> None:
    """Serve the MCP server over stdio (default transport)."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def build_http_app(server: Any) -> Any:
    """Build a Starlette ASGI app serving the MCP server over streamable HTTP.

    Exposes the MCP endpoint at ``/mcp`` and a ``/health`` probe.
    """
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "healthy", "service": "ms365-mcp"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/", health, methods=["GET"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )


async def run_http(server: Any, host: str, port: int) -> None:
    """Serve the MCP server over streamable HTTP via uvicorn."""
    import uvicorn

    logger.info("Starting s365-mcp · HTTP · %s:%s/mcp", host, port)
    config = uvicorn.Config(build_http_app(server), host=host, port=port)
    await uvicorn.Server(config).serve()


# ── Entry point ───────────────────────────────────────────────────────────────


async def _build_client(tenant_override: str | None) -> GraphClient:
    """Build an authenticated GraphClient, running device code flow at boot if needed."""
    import sys

    from ms365.auth import MS365Auth, MS365AuthError
    from ms365.config import get_settings
    from ms365.graph_client import GraphClient

    cfg = get_settings()
    tenant_id = tenant_override or cfg.tenant_id

    if not cfg.client_id:
        sys.exit("Microsoft 365 client_id is not configured. Set PINCER_MS365_CLIENT_ID.")

    auth = MS365Auth(
        client_id=cfg.client_id,
        tenant_id=tenant_id,
        cache_path=str(cfg.cache_path),
        services=cfg.services,
    )

    if not auth.has_cached_token():
        logger.info("No cached token — starting device code flow...")
        try:
            await auth.device_code_flow()
        except MS365AuthError as exc:
            sys.exit(f"Microsoft 365 authentication failed: {exc}")

    return GraphClient(auth)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microsoft 365 MCP server (Pincer)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'stdio' (default) or 'http' (streamable HTTP).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", DEFAULT_HOST),
        help=f"Bind host for HTTP transport (default: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help=f"Bind port for HTTP transport (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--services",
        default="",
        help=f"Comma-separated services to expose (default: all). Options: {', '.join(SERVICES)}.",
    )
    parser.add_argument(
        "--tenant",
        default="",
        help="Override the Microsoft tenant id (default: from config / 'common').",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Expose only read tools; hide tools that modify data (send/delete/etc.).",
    )
    return parser.parse_args()


async def _async_main(client: GraphClient, args: argparse.Namespace) -> None:
    """Async entry point: serve the already-authenticated client."""
    services = [s.strip() for s in args.services.split(",") if s.strip()] or None
    server, specs = build_server(client, services=services, read_only=args.read_only)
    logger.info("Exposing %d Microsoft 365 tool(s) over %s", len(specs), args.transport)

    if args.transport == "http":
        await run_http(server, args.host, args.port)
    else:
        await run_stdio(server)


def _build_client_sync(tenant_override: str | None) -> GraphClient:
    """Build an authenticated GraphClient synchronously (blocking).

    Runs the device code flow in the calling thread — no event loop involved.
    Call this before asyncio.run() so MSAL never crosses thread boundaries.
    """
    import sys

    from ms365.auth import MS365Auth, MS365AuthError
    from ms365.config import get_settings
    from ms365.graph_client import GraphClient

    cfg = get_settings()
    tenant_id = tenant_override or cfg.tenant_id

    if not cfg.client_id:
        sys.exit(
            "Microsoft 365 client_id is not configured. "
            "Set MS365_CLIENT_ID (standalone) or PINCER_MS365_CLIENT_ID in .env (pincer run)."
        )

    auth = MS365Auth(
        client_id=cfg.client_id,
        tenant_id=tenant_id,
        cache_path=str(cfg.cache_path),
        services=cfg.services,
    )

    if not auth.has_cached_token():
        logger.info("No cached token — starting device code flow...")
        try:
            auth.device_code_flow_sync()
        except MS365AuthError as exc:
            sys.exit(f"Microsoft 365 authentication failed: {exc}")

    return GraphClient(auth)


def main() -> None:
    """CLI entry point for the standalone MS365 MCP server."""
    import asyncio

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _parse_args()
    # Authenticate synchronously before starting the event loop so MSAL's
    # device code flow runs in a single thread with no async indirection.
    client = _build_client_sync(args.tenant or None)
    asyncio.run(_async_main(client, args))


if __name__ == "__main__":
    main()
