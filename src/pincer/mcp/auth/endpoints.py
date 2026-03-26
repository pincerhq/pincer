"""OAuth 2.1 HTTP endpoints — /authorize /token /register /revoke."""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from pincer.mcp.auth.consent import render_consent_page
from pincer.mcp.auth.errors import OAuthError, invalid_grant, invalid_request, unsupported_grant_type
from pincer.mcp.auth.models import AuthorizationCode, GrantType
from pincer.mcp.auth.pkce import verify_code_challenge
from pincer.mcp.auth.scopes import get_scope_descriptions, validate_scopes

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from pincer.mcp.auth.clients import ClientRegistry
    from pincer.mcp.auth.tokens import TokenService

logger = logging.getLogger("pincer.mcp.auth.endpoints")

# Pending authorization requests (request_id → params dict), 10-min TTL
_pending: dict[str, dict[str, Any]] = {}


def _cleanup_pending() -> None:
    now = time.time()
    expired = [k for k, v in _pending.items() if v.get("expires_at", 0) < now]
    for k in expired:
        del _pending[k]


def mount_oauth_endpoints(
    app: Any,
    token_service: TokenService,
    client_registry: ClientRegistry,
    consent_handler: Callable[..., Any] | None,
    resource_uri: str,
    issuer: str,
) -> None:
    """
    Register all OAuth 2.1 routes on a Starlette app.

    Routes added:
      GET  /.well-known/oauth-protected-resource
      GET  /.well-known/oauth-authorization-server
      GET  /.well-known/jwks.json
      GET  /authorize
      POST /authorize/consent
      POST /token
      POST /register
      POST /revoke
    """
    from pincer.mcp.auth.metadata import make_metadata_handlers
    from pincer.mcp.auth.scopes import PINCER_SCOPES

    all_scopes = " ".join(PINCER_SCOPES.keys())
    handle_prm, handle_as_meta, handle_jwks = make_metadata_handlers(
        resource_uri=resource_uri,
        issuer=issuer,
        scopes=all_scopes,
        token_service=token_service,
    )

    async def handle_authorize(request: Request) -> Response:
        params = dict(request.query_params)
        try:
            return await _authorize(request, params, token_service, client_registry, issuer, resource_uri)
        except OAuthError as e:
            redirect_uri = params.get("redirect_uri", "")
            state = params.get("state", "")
            if redirect_uri:
                qs = urlencode({"error": e.error, "error_description": e.description, "state": state})
                return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)
            return JSONResponse(e.to_dict(), status_code=e.status_code)

    async def handle_authorize_consent(request: Request) -> Response:
        try:
            form = await request.form()
            request_id = str(form.get("request_id", ""))
            decision = str(form.get("decision", ""))
        except Exception:
            return Response("Bad request", status_code=400)

        _cleanup_pending()
        pending = _pending.pop(request_id, None)
        if not pending or pending.get("expires_at", 0) < time.time():
            return Response("Authorization request expired or not found", status_code=400)

        redirect_uri = pending["redirect_uri"]
        state = pending["state"]

        if decision != "approve":
            qs = urlencode({"error": "access_denied", "error_description": "User denied access", "state": state})
            return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)

        code_str = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code_str,
            client_id=pending["client_id"],
            redirect_uri=redirect_uri,
            code_challenge=pending["code_challenge"],
            scope=pending["scope"],
            resource=pending["resource"],
            expires_at=int(time.time()) + 600,
        )
        token_service.store_authorization_code(auth_code)
        qs = urlencode({"code": code_str, "state": state})
        return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)

    async def handle_token(request: Request) -> JSONResponse:
        try:
            form = await request.form()
            grant_type = str(form.get("grant_type", ""))
            return await _token(form, grant_type, token_service, client_registry, resource_uri)
        except OAuthError as e:
            return JSONResponse(e.to_dict(), status_code=e.status_code)

    async def handle_register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_request", "error_description": "Invalid JSON"}, status_code=400)
        try:
            client = client_registry.register(
                client_name=str(body.get("client_name", "")),
                redirect_uris=list(body.get("redirect_uris", [])),
                grant_types=list(body.get("grant_types", ["authorization_code"])),
                auth_method=str(body.get("token_endpoint_auth_method", "none")),
            )
            response: dict[str, Any] = {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uris": client.redirect_uris,
                "grant_types": client.grant_types,
                "token_endpoint_auth_method": client.token_endpoint_auth_method,
            }
            if client.client_secret:
                response["client_secret"] = client.client_secret
            return JSONResponse(response, status_code=201)
        except OAuthError as e:
            return JSONResponse(e.to_dict(), status_code=e.status_code)

    async def handle_revoke(request: Request) -> Response:
        try:
            form = await request.form()
            token = str(form.get("token", ""))
            if token:
                token_service.revoke(token)
        except Exception:
            pass  # RFC 7009: always return 200
        return Response(status_code=200)

    # Register routes
    routes_to_add = [
        ("/.well-known/oauth-protected-resource", handle_prm, ["GET"]),
        ("/.well-known/oauth-authorization-server", handle_as_meta, ["GET"]),
        ("/.well-known/jwks.json", handle_jwks, ["GET"]),
        ("/authorize", handle_authorize, ["GET"]),
        ("/authorize/consent", handle_authorize_consent, ["POST"]),
        ("/token", handle_token, ["POST"]),
        ("/register", handle_register, ["POST"]),
        ("/revoke", handle_revoke, ["POST"]),
    ]

    # Starlette apps expose add_route; FastAPI uses add_api_route
    for path, handler, methods in routes_to_add:
        app.add_route(path, handler, methods=methods)


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _authorize(
    request: Request,
    params: dict[str, Any],
    token_service: TokenService,
    client_registry: ClientRegistry,
    issuer: str,
    resource_uri: str,
) -> Response:
    response_type = params.get("response_type", "")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    state = params.get("state", "")
    resource = params.get("resource", resource_uri)
    scope = params.get("scope", "tools:read")

    if not state:
        raise invalid_request("state parameter is required")
    if response_type != "code":
        raise invalid_request("Only response_type=code is supported")
    if not client_id:
        raise invalid_request("client_id is required")
    if not redirect_uri:
        raise invalid_request("redirect_uri is required")
    if not code_challenge:
        raise invalid_request("code_challenge is required (PKCE mandatory)")
    if code_challenge_method != "S256":
        raise invalid_request("Only code_challenge_method=S256 is supported")

    client = client_registry.get(client_id)
    if not client:
        raise invalid_request(f"Unknown client_id: {client_id!r}")

    if not client_registry.validate_redirect_uri(client_id, redirect_uri):
        raise invalid_request(f"Redirect URI not registered: {redirect_uri!r}")

    if "authorization_code" not in client.grant_types:
        from pincer.mcp.auth.errors import unauthorized_client
        raise unauthorized_client("Client does not support authorization_code grant")

    # Validate/clamp scope
    from pincer.mcp.auth.scopes import PINCER_SCOPES

    all_scopes = " ".join(PINCER_SCOPES.keys())
    if not validate_scopes(scope, all_scopes):
        raise invalid_request(f"Unknown scope(s): {scope!r}")

    if client.auto_consent:
        code_str = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code_str,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=scope,
            resource=resource,
            expires_at=int(time.time()) + 600,
        )
        token_service.store_authorization_code(auth_code)
        qs = urlencode({"code": code_str, "state": state})
        return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)

    # Show consent page
    _cleanup_pending()
    request_id = secrets.token_hex(16)
    _pending[request_id] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "resource": resource,
        "state": state,
        "expires_at": time.time() + 600,
    }

    scope_descs = get_scope_descriptions(scope)
    html_content = render_consent_page(
        client_name=client.client_name,
        scope_descriptions=scope_descs,
        form_action="/authorize/consent",
        request_id=request_id,
    )
    return HTMLResponse(html_content)


async def _token(
    form: Any,
    grant_type: str,
    token_service: TokenService,
    client_registry: ClientRegistry,
    resource_uri: str,
) -> JSONResponse:
    if grant_type == GrantType.AUTHORIZATION_CODE:
        return await _token_code(form, token_service, client_registry, resource_uri)
    elif grant_type == GrantType.CLIENT_CREDENTIALS:
        return await _token_client_credentials(form, token_service, client_registry, resource_uri)
    elif grant_type == GrantType.REFRESH_TOKEN:
        return await _token_refresh(form, token_service)
    else:
        raise unsupported_grant_type()


async def _token_code(
    form: Any,
    token_service: TokenService,
    client_registry: ClientRegistry,
    resource_uri: str,
) -> JSONResponse:
    code = str(form.get("code", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    client_id = str(form.get("client_id", ""))
    code_verifier = str(form.get("code_verifier", ""))
    resource = str(form.get("resource", resource_uri))

    if not code:
        raise invalid_request("code is required")
    if not redirect_uri:
        raise invalid_request("redirect_uri is required")
    if not client_id:
        raise invalid_request("client_id is required")
    if not code_verifier:
        raise invalid_request("code_verifier is required (PKCE mandatory)")

    auth_code = token_service.get_authorization_code(code)
    if not auth_code:
        raise invalid_grant("Authorization code not found")
    if auth_code.used:
        raise invalid_grant("Authorization code already used")
    if auth_code.expires_at < int(time.time()):
        raise invalid_grant("Authorization code has expired")
    if auth_code.client_id != client_id:
        raise invalid_grant("Client mismatch")
    if auth_code.redirect_uri != redirect_uri:
        raise invalid_grant("redirect_uri mismatch")

    try:
        if not verify_code_challenge(code_verifier, auth_code.code_challenge):
            raise invalid_grant("PKCE verification failed")
    except ValueError as e:
        raise invalid_request(str(e)) from e

    # Mark code as used (replace with used=True copy)
    used_code = AuthorizationCode(
        code=auth_code.code,
        client_id=auth_code.client_id,
        redirect_uri=auth_code.redirect_uri,
        code_challenge=auth_code.code_challenge,
        scope=auth_code.scope,
        resource=auth_code.resource,
        expires_at=auth_code.expires_at,
        used=True,
    )
    token_service.store_authorization_code(used_code)

    access_token = token_service.issue_access_token(client_id, resource, auth_code.scope)

    response: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": token_service._access_lifetime,
        "scope": auth_code.scope,
    }

    # Issue refresh token only if offline_access scope requested
    client = client_registry.get(client_id)
    if client and "offline_access" in auth_code.scope.split() and "refresh_token" in (client.grant_types or []):
        response["refresh_token"] = token_service.issue_refresh_token(client_id, auth_code.scope, resource)

    return JSONResponse(response)


async def _token_client_credentials(
    form: Any,
    token_service: TokenService,
    client_registry: ClientRegistry,
    resource_uri: str,
) -> JSONResponse:
    client_id = str(form.get("client_id", ""))
    client_secret = str(form.get("client_secret", ""))
    scope = str(form.get("scope", "tools:read"))
    resource = str(form.get("resource", resource_uri))

    if not client_id:
        raise invalid_request("client_id is required")
    if not client_secret:
        raise invalid_request("client_secret is required for client_credentials")

    if not client_registry.verify_secret(client_id, client_secret):
        from pincer.mcp.auth.errors import invalid_client
        raise invalid_client("Invalid client credentials")

    client = client_registry.get(client_id)
    if not client:
        from pincer.mcp.auth.errors import invalid_client
        raise invalid_client("Unknown client")

    if "client_credentials" not in client.grant_types:
        from pincer.mcp.auth.errors import unauthorized_client
        raise unauthorized_client("Client does not support client_credentials grant")

    access_token = token_service.issue_access_token(client_id, resource, scope)
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": token_service._access_lifetime,
            "scope": scope,
        }
    )


async def _token_refresh(form: Any, token_service: TokenService) -> JSONResponse:
    refresh_token = str(form.get("refresh_token", ""))
    client_id = str(form.get("client_id", ""))

    if not refresh_token:
        raise invalid_request("refresh_token is required")
    if not client_id:
        raise invalid_request("client_id is required")

    new_access, new_refresh = token_service.rotate_refresh_token(refresh_token, client_id)
    return JSONResponse(
        {
            "access_token": new_access,
            "token_type": "Bearer",
            "expires_in": token_service._access_lifetime,
            "refresh_token": new_refresh,
        }
    )
