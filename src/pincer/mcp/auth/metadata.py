"""RFC 8414 Authorization Server Metadata + RFC 9728 Protected Resource Metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request


def build_protected_resource_metadata(resource_uri: str, issuer: str, scopes: str) -> dict[str, Any]:
    """RFC 9728 — Protected Resource Metadata."""
    return {
        "resource": resource_uri,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": scopes.split(),
    }


def build_authorization_server_metadata(issuer: str, scopes: str) -> dict[str, Any]:
    """RFC 8414 — Authorization Server Metadata."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "revocation_endpoint": f"{issuer}/revoke",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "scopes_supported": scopes.split(),
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "client_credentials",
            "refresh_token",
        ],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
    }


def make_metadata_handlers(
    resource_uri: str,
    issuer: str,
    scopes: str,
    token_service: Any,
) -> tuple[Any, Any, Any]:
    """
    Return (handle_protected_resource, handle_authorization_server, handle_jwks).
    Uses closures to capture config.
    """

    async def handle_protected_resource(request: Request) -> JSONResponse:
        return JSONResponse(
            build_protected_resource_metadata(resource_uri, issuer, scopes),
            headers={"Cache-Control": "no-store"},
        )

    async def handle_authorization_server(request: Request) -> JSONResponse:
        return JSONResponse(
            build_authorization_server_metadata(issuer, scopes),
            headers={"Cache-Control": "no-store"},
        )

    async def handle_jwks(request: Request) -> JSONResponse:
        jwk = token_service.get_public_key_jwk()
        return JSONResponse({"keys": [jwk]}, headers={"Cache-Control": "no-store"})

    return handle_protected_resource, handle_authorization_server, handle_jwks
