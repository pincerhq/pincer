"""
Standalone Microsoft 365 MCP server.

Exposes Microsoft 365 tools (Outlook email, Calendar, OneDrive, To Do,
Contacts, OneNote) as an MCP server so any MCP host — Claude Desktop,
Cursor, or Pincer's own MCP client — can drive Microsoft Graph.

Transports::

    ms365-mcp-run --transport stdio                         # default
    ms365-mcp-run --transport http --host 127.0.0.1 --port 8000
        MCP endpoint → http://127.0.0.1:8000/mcp

Authentication is per-identity and lazy: set ``MS365_CLIENT_ID`` (and
optionally ``MS365_TENANT_ID``) then start the server — no auth happens at
boot. Each distinct caller ``identity`` (an opaque string carried in the MCP
call's ``_meta``, see ``_identity_from_ctx``) authenticates on its own first
tool call, with its own token cache at
``<MS365_TOKEN_CACHE_DIR>/<identity>_token_cache.json`` (default
``~/.pincer/ms365/``). See ``ms365/identity_session.py``.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp import FastMCP

    from ms365._registry import ClientResolver
    from ms365.graph_client import GraphClient
    from ms365.identity_session import IdentitySessionManager

from ms365.config import get_settings

logging.basicConfig(level=getattr(logging, get_settings().log_level.value))
logger = logging.getLogger("ms365.ms365_server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
SERVICES = ("email", "calendar", "onedrive", "todo", "contacts", "onenote", "directory")


def _registrars() -> dict[str, Callable[..., int]]:
    """Map each service to the register function that emits its tools."""
    from ms365.tools_calendar import register_calendar_tools
    from ms365.tools_contacts import register_contacts_tools
    from ms365.tools_directory import register_directory_tools
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
        "directory": register_directory_tools,
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
    resolve_client: ClientResolver,
    services: list[str] | None = None,
    read_only: bool = False,
) -> list[_ToolSpec]:
    """Collect tool specs for the requested services by reusing the registrars.

    Args:
        resolve_client: Resolves the caller's identity (from `ctx`) to that
            identity's authenticated GraphClient — see `ms365/identity_session.py`.
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
        register_fn(registry, resolve_client)

    if read_only:
        return [s for s in registry.specs if not s.require_approval]
    return registry.specs


def _resolve_annotation(raw: Any, globalns: dict[str, Any]) -> Any:
    """Best-effort resolve a (possibly stringified) annotation to a real type.

    ``tools_*.py`` use ``from __future__ import annotations``, so every
    parameter annotation on the original tool functions is a plain string at
    runtime (e.g. ``"str"``, ``"bool"``). Evaluate it against the defining
    module's globals; fall back to ``Any`` if it can't be resolved (e.g. a
    forward reference like ``GraphClient`` that's only imported under
    ``TYPE_CHECKING`` and isn't needed once ``client`` has been dropped).
    """
    if not isinstance(raw, str):
        return raw
    try:
        return eval(raw, globalns)  # noqa: S307 — trusted first-party annotation strings
    except NameError:
        return Any


def _with_exposed_signature(spec: _ToolSpec) -> Callable[..., Any]:
    """Build a thin wrapper FastMCP can introspect for the tool's JSON schema.

    ``register_*_tools()`` wraps every tool as ``async def wrapper(ctx, **kwargs)``
    via ``functools.wraps(fn)``, which also copies ``fn.__annotations__``
    verbatim — including the ``client: GraphClient`` parameter, whose type is
    only available under ``TYPE_CHECKING``. FastMCP/pydantic eagerly resolve
    every entry in ``__annotations__``, which would crash trying to look up
    ``GraphClient`` at runtime. Build a fresh function exposing only the
    already-bound handler's real, non-``client`` parameters with concrete
    (non-string) annotations, plus a ``ctx: Context`` parameter so FastMCP
    auto-injects the caller's per-call Context (used to resolve `identity`)
    without it ever appearing in the tool's public JSON schema.
    """
    handler = spec.handler
    original = getattr(handler, "__wrapped__", None)
    if original is None:
        return handler

    sig = inspect.signature(original)
    globalns = getattr(original, "__globals__", {})
    kept_params = [p for p in sig.parameters.values() if p.name != "client"]
    resolved_params = [p.replace(annotation=_resolve_annotation(p.annotation, globalns)) for p in kept_params]
    return_annotation = _resolve_annotation(sig.return_annotation, globalns)

    ctx_param = inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    all_params = [ctx_param, *resolved_params]

    async def tool_fn(**kwargs: Any) -> str:
        return await handler(**kwargs)

    tool_fn.__name__ = spec.name
    tool_fn.__doc__ = spec.description
    tool_fn.__signature__ = sig.replace(parameters=all_params, return_annotation=return_annotation)  # type: ignore[attr-defined]
    tool_fn.__annotations__ = {
        "ctx": Context,
        **{p.name: p.annotation for p in resolved_params if p.annotation is not inspect.Parameter.empty},
    }
    if return_annotation is not inspect.Signature.empty:
        tool_fn.__annotations__["return"] = return_annotation
    return tool_fn


def build_server(
    resolve_client: ClientResolver,
    services: list[str] | None = None,
    read_only: bool = False,
) -> tuple[FastMCP, list[_ToolSpec]]:
    """Build a FastMCP app exposing the collected MS365 tools.

    Returns the FastMCP instance and the list of specs it serves.
    """
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from fastmcp.tools.tool import ToolAnnotations

    specs = collect_tools(resolve_client, services=services, read_only=read_only)

    mcp = FastMCP("pincer-ms365")
    for spec in specs:
        mcp.add_tool(
            Tool.from_function(
                _with_exposed_signature(spec),
                name=spec.name,
                description=spec.description,
                annotations=ToolAnnotations(
                    readOnlyHint=not spec.require_approval,
                    destructiveHint=spec.require_approval,
                ),
            )
        )

    logger.debug("MS365 MCP server built with %d tool(s)", len(specs))
    return mcp, specs


# ── Transports ────────────────────────────────────────────────────────────────


async def run_stdio(mcp: FastMCP) -> None:
    """Serve the MCP server over stdio (default transport)."""
    # show_banner=False: the banner (and its PyPI version check) would otherwise
    # print straight to stderr on every launch and make an unnecessary network call.
    await mcp.run_async(transport="stdio", show_banner=False)


def build_http_app(mcp: FastMCP) -> Any:
    """Build a Starlette ASGI app serving the MCP server over streamable HTTP.

    Exposes the MCP endpoint at ``/mcp`` and a ``/health`` probe.

    Note: this bypasses FastMCP's own ``run_http_async`` (which is what prints
    the startup banner) in favor of building the app and serving it with our
    own uvicorn config, so no banner-suppression flag is needed here.
    """
    from starlette.responses import JSONResponse

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "healthy", "service": "ms365-mcp"})

    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/", methods=["GET"])(health)

    return mcp.http_app(path="/mcp", transport="http")


async def run_http(mcp: FastMCP, host: str, port: int) -> None:
    """Serve the MCP server over streamable HTTP via uvicorn."""
    import uvicorn

    logger.info("Starting ms365-mcp · HTTP · %s:%s/mcp", host, port)
    config = uvicorn.Config(build_http_app(mcp), host=host, port=port)
    await uvicorn.Server(config).serve()


# ── Entry point ───────────────────────────────────────────────────────────────


def _identity_from_ctx(ctx: Context) -> str | None:
    """Read the caller's opaque `identity` out of the MCP call's `_meta` envelope.

    Returns None if absent (e.g. a bare MCP client that doesn't send one) —
    IdentitySessionManager.get_or_create() falls back to a fixed default slot.
    """
    request_context = ctx.request_context
    meta = getattr(request_context, "meta", None) if request_context is not None else None
    return getattr(meta, "identity", None) if meta is not None else None


def _make_client_resolver(manager: IdentitySessionManager) -> ClientResolver:
    """Bind a manager into the `ClientResolver` shape tool registration expects."""

    async def resolve_client(ctx: Context) -> GraphClient:
        return await manager.get_or_create(_identity_from_ctx(ctx), ctx=ctx)

    return resolve_client


def _register_auth_status_tool(mcp: FastMCP, manager: IdentitySessionManager) -> None:
    """Register a transport-agnostic fallback for checking sign-in status.

    The background device-code poll pushes a best-effort completion
    notification (see `identity_session.py`'s module docstring for why this
    isn't guaranteed to reach every transport/client) — this tool lets any
    caller poll instead, regardless of transport.
    """
    from fastmcp.tools import Tool
    from fastmcp.tools.tool import ToolAnnotations

    async def ms365__check_auth_status(ctx: Context) -> str:
        """Check Microsoft 365 sign-in status: signed in, sign-in pending, or not signed in yet."""
        identity = _identity_from_ctx(ctx)
        return manager.status_for(identity).message

    mcp.add_tool(
        Tool.from_function(
            ms365__check_auth_status,
            name="ms365__check_auth_status",
            description="Check Microsoft 365 sign-in status for the caller.",
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        )
    )


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


async def _async_main(args: argparse.Namespace) -> None:
    """Async entry point: build the identity-aware server and serve it.

    No boot-time authentication happens here anymore — each identity
    authenticates lazily on its first tool call (see `identity_session.py`).
    """
    import sys

    from ms365.config import get_settings
    from ms365.crypto import load_or_generate_fernet
    from ms365.identity_session import IdentitySessionManager

    cfg = get_settings()
    if not cfg.client_id:
        sys.exit(
            "Microsoft 365 client_id is not configured. "
            "Set MS365_CLIENT_ID (standalone) or PINCER_MS365_CLIENT_ID in .env (pincer run)."
        )

    services = [s.strip() for s in args.services.split(",") if s.strip()] or None
    tenant_id = args.tenant or cfg.tenant_id

    # Note: OAuth scopes are derived from `cfg.services` (the full configured
    # set), not the `--services` tool-exposure filter above — matches today's
    # existing behavior where a narrower `--services` only hides tools, it
    # doesn't shrink the consent scope requested per identity.
    fernet = load_or_generate_fernet(cfg)
    manager = IdentitySessionManager(
        client_id=cfg.client_id,
        tenant_id=tenant_id,
        cache_dir=cfg.token_cache_dir,
        services=cfg.services,
        fernet=fernet,
    )
    resolve_client = _make_client_resolver(manager)

    server, specs = build_server(resolve_client, services=services, read_only=args.read_only)
    _register_auth_status_tool(server, manager)
    logger.info("Exposing %d Microsoft 365 tool(s) over %s", len(specs), args.transport)

    if args.transport == "http":
        await run_http(server, args.host, args.port)
    else:
        await run_stdio(server)


def main() -> None:
    """CLI entry point for the standalone MS365 MCP server."""
    import asyncio

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
