"""ASGI middleware for Bearer token validation on the MCP endpoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from pincer.mcp.auth.errors import OAuthError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from pincer.mcp.auth.tokens import TokenService

logger = logging.getLogger("pincer.mcp.auth.middleware")

# Paths that are always exempt from token validation
_EXEMPT_PREFIXES = (
    "/.well-known/",
)
_EXEMPT_EXACT = {
    "/authorize",
    "/authorize/consent",
    "/token",
    "/register",
    "/revoke",
}

_LOCALHOST_ADDRS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforces OAuth 2.1 Bearer tokens on all non-exempt paths.

    When localhost_bypass=True (default), requests from 127.0.0.1/::1 skip
    authentication entirely — existing local users are unaffected.

    The 401 WWW-Authenticate header with resource_metadata is the MCP OAuth
    discovery entry point. Claude Desktop reads this to find the auth server.

    Legacy mode: pass auth_provider=<MCPAuthProvider> to use the old HS256
    middleware behavior (backward compat with pre-Sprint 8 code).
    """

    def __init__(
        self,
        app: ASGIApp,
        token_service: TokenService | None = None,
        resource_uri: str = "",
        issuer: str = "",
        localhost_bypass: bool = True,
        # Backward-compat keyword kept from the legacy MCPAuthMiddleware
        auth_provider: Any = None,
    ) -> None:
        super().__init__(app)
        self._token_service = token_service
        self._resource_uri = resource_uri
        self._issuer = issuer
        self._localhost_bypass = localhost_bypass
        self._legacy_provider = auth_provider  # may be None

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Legacy HS256 auth_provider mode
        if self._legacy_provider is not None:
            return await self._dispatch_legacy(request, call_next)

        path = request.url.path

        # Exempt OAuth and well-known paths
        if path in _EXEMPT_EXACT or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # Localhost bypass
        if self._localhost_bypass:
            client_host = request.client.host if request.client else ""
            if client_host in _LOCALHOST_ADDRS:
                request.state.auth_claims = None
                request.state.auth_bypassed = True
                return await call_next(request)

        # Require Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            prm_uri = f"{self._issuer}/.well-known/oauth-protected-resource"
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{prm_uri}"'},
            )

        token = auth_header[7:]
        if self._token_service is None:
            return await call_next(request)
        try:
            claims = self._token_service.validate_access_token(token, self._resource_uri)
        except OAuthError as e:
            prm_uri = f"{self._issuer}/.well-known/oauth-protected-resource"
            status = e.status_code
            return JSONResponse(
                e.to_dict(),
                status_code=status,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{prm_uri}"'},
            )

        request.state.auth_claims = claims
        request.state.auth_bypassed = False
        return await call_next(request)

    async def _dispatch_legacy(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Backward-compat dispatch using the old MCPAuthProvider (HS256)."""
        provider = self._legacy_provider
        client_host = request.client.host if request.client else ""

        if provider.is_localhost(client_host):
            return await call_next(request)

        if request.url.path == "/oauth/token" and request.method == "POST":
            try:
                form = await request.form()
                token = provider.issue_token(str(form.get("client_id", "")), str(form.get("client_secret", "")))
            except Exception:
                token = None
            if token:
                return JSONResponse(
                    {"access_token": token, "token_type": "Bearer", "expires_in": provider._token_expiry}
                )
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Bearer token required"},
                status_code=401,
            )
        claims = provider.validate_token(auth_header[7:])
        if not claims:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Token invalid or expired"},
                status_code=401,
            )

        request.state.mcp_client = claims.get("sub")
        return await call_next(request)
