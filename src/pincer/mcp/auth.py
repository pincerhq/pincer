"""
OAuth 2.1 provider for Pincer's MCP server endpoint.

Pincer acts as both Authorization Server and Resource Server for its MCP
endpoint. Simplified for single-user personal agent:
- client_credentials grant only (no user consent flow)
- JWT access tokens with configurable expiry
- Client whitelist from config
- Localhost connections bypass auth entirely
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = logging.getLogger("pincer.mcp.auth")


@dataclass
class OAuthClient:
    client_id: str
    client_secret: str
    name: str


class MCPAuthProvider:
    """
    OAuth 2.1 provider for Pincer's MCP server.

    Issues JWT access tokens for pre-registered clients using the
    client_credentials grant. Localhost connections bypass auth.
    """

    def __init__(
        self,
        allowed_clients: list[dict[str, Any]],
        token_expiry_seconds: int = 3600,
        signing_key: str | None = None,
    ) -> None:
        self._signing_key = signing_key or secrets.token_hex(32)
        self._token_expiry = token_expiry_seconds
        self._clients: dict[str, OAuthClient] = {}
        self._revoked_tokens: set[str] = set()

        for client_def in allowed_clients:
            client = OAuthClient(
                client_id=client_def["client_id"],
                client_secret=client_def.get("client_secret", secrets.token_hex(32)),
                name=client_def.get("name", client_def["client_id"]),
            )
            self._clients[client.client_id] = client

    def issue_token(self, client_id: str, client_secret: str) -> str | None:
        """Validate client credentials and issue a JWT. Returns None on failure."""
        try:
            import jwt
        except ImportError as e:
            raise RuntimeError(
                "PyJWT required for MCP auth: uv pip install 'pincer-agent[mcp]'"
            ) from e

        client = self._clients.get(client_id)
        if not client:
            logger.warning("OAuth: unknown client_id '%s'", client_id)
            return None
        if client.client_secret != client_secret:
            logger.warning("OAuth: invalid secret for client '%s'", client_id)
            return None

        now = int(time.time())
        payload = {
            "sub": client_id,
            "name": client.name,
            "iat": now,
            "exp": now + self._token_expiry,
            "jti": secrets.token_hex(16),
        }
        token: str = jwt.encode(payload, self._signing_key, algorithm="HS256")
        logger.info("OAuth: issued token for client '%s'", client_id)
        return token

    def validate_token(self, token: str) -> dict[str, Any] | None:
        """Validate a JWT and return its claims. Returns None if invalid."""
        try:
            import jwt
        except ImportError:
            return None

        try:
            claims: dict[str, Any] = jwt.decode(
                token, self._signing_key, algorithms=["HS256"]
            )
            if claims.get("jti") in self._revoked_tokens:
                logger.debug("OAuth: revoked token presented")
                return None
            return claims
        except jwt.ExpiredSignatureError:
            logger.debug("OAuth: expired token")
            return None
        except jwt.InvalidTokenError as exc:
            logger.debug("OAuth: invalid token — %s", exc)
            return None

    def revoke_token(self, jti: str) -> None:
        """Revoke a token by its JTI claim."""
        self._revoked_tokens.add(jti)

    def is_localhost(self, remote_addr: str) -> bool:
        """Localhost requests bypass OAuth entirely."""
        return remote_addr in ("127.0.0.1", "::1", "localhost")


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces OAuth bearer tokens on the MCP endpoint.

    - Localhost requests bypass auth automatically.
    - POST /oauth/token issues access tokens (client_credentials grant).
    - All other requests must present a valid Bearer token.
    """

    def __init__(self, app: ASGIApp, auth_provider: MCPAuthProvider) -> None:
        super().__init__(app)
        self.auth_provider = auth_provider

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        client_host = request.client.host if request.client else "unknown"

        # Localhost bypass
        if self.auth_provider.is_localhost(client_host):
            return await call_next(request)

        # Token issuance endpoint
        if request.url.path == "/oauth/token" and request.method == "POST":
            return await self._handle_token_request(request)

        # Validate bearer token on all other requests
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Bearer token required"},
                status_code=401,
            )

        token = auth_header[7:]
        claims = self.auth_provider.validate_token(token)
        if not claims:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Token invalid or expired"},
                status_code=401,
            )

        request.state.mcp_client = claims["sub"]
        return await call_next(request)

    async def _handle_token_request(self, request: Request) -> JSONResponse:
        """Handle POST /oauth/token (client_credentials grant)."""
        try:
            form = await request.form()
            grant_type = form.get("grant_type", "")
            client_id = form.get("client_id", "")
            client_secret = form.get("client_secret", "")
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        if grant_type != "client_credentials":
            return JSONResponse(
                {"error": "unsupported_grant_type"},
                status_code=400,
            )

        if not client_id or not client_secret:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        token = self.auth_provider.issue_token(str(client_id), str(client_secret))
        if token is None:
            logger.warning("OAuth: denied token request for client '%s'", client_id)
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": self.auth_provider._token_expiry,
        })
