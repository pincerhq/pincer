"""
Pincer MCP OAuth 2.1 — Authorization Server + Client.

Public API
----------
Server-side (Resource Server + Authorization Server):
    AuthServerConfig   — configuration dataclass
    TokenService       — JWT issue/validate/revoke
    ClientRegistry     — client storage + DCR
    MCPAuthMiddleware  — Bearer token enforcement middleware
    mount_oauth_endpoints — register all OAuth routes on a Starlette app

Client-side (Pincer connecting to an OAuth-protected MCP server):
    MCPOAuthClient     — PKCE + client_credentials + refresh flows
    TokenStore         — persistent token storage

Shared models:
    OAuthClient, TokenClaims

Backward-compat (legacy HS256 provider):
    MCPAuthProvider    — simple HMAC-based provider (pre-Sprint 8)
"""

from __future__ import annotations

import logging as _logging
import secrets as _secrets
import time as _time
from dataclasses import dataclass as _dataclass
from typing import Any as _Any

from pincer.mcp.auth.client_flow import MCPOAuthClient
from pincer.mcp.auth.clients import ClientRegistry
from pincer.mcp.auth.endpoints import mount_oauth_endpoints
from pincer.mcp.auth.middleware import MCPAuthMiddleware
from pincer.mcp.auth.models import AuthServerConfig, OAuthClient, TokenClaims
from pincer.mcp.auth.token_store import TokenStore
from pincer.mcp.auth.tokens import TokenService

# ── Backward-compatibility shim ───────────────────────────────────────────────
# The pre-Sprint 8 MCPAuthProvider used HS256 HMAC signing and a client
# whitelist. Kept here so existing code/tests continue to import correctly.

_compat_logger = _logging.getLogger("pincer.mcp.auth.legacy")


@_dataclass
class _LegacyOAuthClient:
    """Legacy OAuthClient (client_id, client_secret, name)."""

    client_id: str
    client_secret: str
    name: str


class MCPAuthProvider:
    """
    Legacy OAuth 2.0 provider (HS256, client_credentials only).

    Retained for backward compatibility. New code should use
    TokenService + MCPAuthMiddleware + mount_oauth_endpoints instead.
    """

    def __init__(
        self,
        allowed_clients: list[dict[_Any, _Any]],
        token_expiry_seconds: int = 3600,
        signing_key: str | None = None,
    ) -> None:
        self._signing_key = signing_key or _secrets.token_hex(32)
        self._token_expiry = token_expiry_seconds
        self._clients: dict[str, _LegacyOAuthClient] = {}
        self._revoked_tokens: set[str] = set()

        for client_def in allowed_clients:
            client = _LegacyOAuthClient(
                client_id=client_def["client_id"],
                client_secret=client_def.get("client_secret", _secrets.token_hex(32)),
                name=client_def.get("name", client_def["client_id"]),
            )
            self._clients[client.client_id] = client

    def issue_token(self, client_id: str, client_secret: str) -> str | None:
        try:
            import jwt
        except ImportError as e:
            raise RuntimeError("PyJWT required for MCP auth") from e

        client = self._clients.get(client_id)
        if not client:
            return None
        if client.client_secret != client_secret:
            return None

        now = int(_time.time())
        payload = {
            "sub": client_id,
            "name": client.name,
            "iat": now,
            "exp": now + self._token_expiry,
            "jti": _secrets.token_hex(16),
        }
        token: str = jwt.encode(payload, self._signing_key, algorithm="HS256")
        return token

    def validate_token(self, token: str) -> dict[str, _Any] | None:
        try:
            import jwt
        except ImportError:
            return None
        try:
            claims: dict[str, _Any] = jwt.decode(token, self._signing_key, algorithms=["HS256"])
            if claims.get("jti") in self._revoked_tokens:
                return None
            return claims
        except Exception:
            return None

    def revoke_token(self, jti: str) -> None:
        self._revoked_tokens.add(jti)

    def is_localhost(self, remote_addr: str) -> bool:
        return remote_addr in ("127.0.0.1", "::1", "localhost")


__all__ = [
    # New OAuth 2.1
    "AuthServerConfig",
    "ClientRegistry",
    "MCPAuthMiddleware",
    "MCPOAuthClient",
    "OAuthClient",
    "TokenClaims",
    "TokenService",
    "TokenStore",
    "mount_oauth_endpoints",
    # Legacy
    "MCPAuthProvider",
]
