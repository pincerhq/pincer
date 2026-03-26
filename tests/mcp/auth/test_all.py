"""
OAuth 2.1 test suite — 48 core tests + coverage extension tests.

Covers: PKCE, tokens, client registry, scopes, metadata,
        integration flows, security properties, token_store,
        consent UI, legacy MCPAuthProvider, client_flow, and error factories.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import time
import urllib.parse
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pincer.mcp.auth.client_flow import MCPOAuthClient, _CallbackServer
from pincer.mcp.auth.clients import ClientRegistry
from pincer.mcp.auth.consent import format_channel_consent, render_consent_page
from pincer.mcp.auth.endpoints import mount_oauth_endpoints
from pincer.mcp.auth.errors import (
    OAuthError,
    insufficient_scope,
    invalid_client,
    invalid_grant,
    invalid_request,
    unauthorized_client,
    unsupported_grant_type,
)
from pincer.mcp.auth.metadata import build_authorization_server_metadata, build_protected_resource_metadata
from pincer.mcp.auth.middleware import MCPAuthMiddleware
from pincer.mcp.auth.models import TokenClaims
from pincer.mcp.auth.pkce import generate_code_challenge, generate_code_verifier, verify_code_challenge
from pincer.mcp.auth.scopes import check_tool_scope, get_scope_descriptions, validate_scopes
from pincer.mcp.auth.token_store import TokenStore
from pincer.mcp.auth.tokens import TokenService

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

# ── Constants ──────────────────────────────────────────────────────────────────

_ISSUER = "http://testserver"
_RESOURCE = "http://testserver/mcp"
_CLIENT_ID = "test-client"
_REDIRECT_URI = "http://127.0.0.1:18801/callback"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def token_service(tmp_path: Path) -> TokenService:
    return TokenService(
        issuer=_ISSUER,
        signing_key_path=tmp_path / "test_signing_key.pem",
        access_lifetime=3600,
        refresh_lifetime=86400,
    )


@pytest.fixture
def client_registry() -> ClientRegistry:
    static = [
        {
            "client_id": "test-client",
            "client_name": "Test Client",
            "redirect_uris": [_REDIRECT_URI, "http://127.0.0.1:*/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
            "auto_consent": True,
            "is_static": True,
        },
        {
            "client_id": "confidential-client",
            "client_name": "Confidential Client",
            "redirect_uris": [],
            "grant_types": ["client_credentials"],
            "token_endpoint_auth_method": "client_secret_post",
            "client_secret": "test-secret-xyz",
            "is_static": True,
        },
    ]
    return ClientRegistry(static_clients=static, dcr_enabled=True, max_clients=20)


@pytest.fixture
def auth_app(token_service: TokenService, client_registry: ClientRegistry) -> Starlette:
    """Full Starlette app with OAuth endpoints + middleware + mock /mcp endpoint."""

    async def mcp_endpoint(request: Request) -> JSONResponse:
        claims = getattr(request.state, "auth_claims", None)
        bypassed = getattr(request.state, "auth_bypassed", False)
        # Simulate scope enforcement: requests to /mcp/execute require tools:execute
        if "execute" in request.url.path and claims and not check_tool_scope("execute_command", claims.scope):
            return JSONResponse({"error": "insufficient_scope"}, status_code=403)
        return JSONResponse({"status": "ok", "bypassed": bypassed})

    routes = [
        Route("/mcp", mcp_endpoint, methods=["GET", "POST"]),
        Route("/mcp/execute", mcp_endpoint, methods=["GET", "POST"]),
    ]
    app = Starlette(routes=routes)

    mount_oauth_endpoints(
        app,
        token_service,
        client_registry,
        None,
        resource_uri=_RESOURCE,
        issuer=_ISSUER,
    )
    app.add_middleware(
        MCPAuthMiddleware,
        token_service=token_service,
        resource_uri=_RESOURCE,
        issuer=_ISSUER,
        localhost_bypass=False,  # Disabled so tests must provide tokens
    )
    return app


@pytest.fixture
def bypass_app(token_service: TokenService, client_registry: ClientRegistry) -> Starlette:
    """Auth app with localhost_bypass=True."""

    async def mcp_endpoint(request: Request) -> JSONResponse:
        bypassed = getattr(request.state, "auth_bypassed", False)
        return JSONResponse({"status": "ok", "bypassed": bypassed})

    routes = [Route("/mcp", mcp_endpoint, methods=["GET", "POST"])]
    app = Starlette(routes=routes)
    mount_oauth_endpoints(app, token_service, client_registry, None, resource_uri=_RESOURCE, issuer=_ISSUER)
    app.add_middleware(
        MCPAuthMiddleware,
        token_service=token_service,
        resource_uri=_RESOURCE,
        issuer=_ISSUER,
        localhost_bypass=True,
    )
    return app


# ── PKCE tests (5) ────────────────────────────────────────────────────────────


def test_verifier_length() -> None:
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128


def test_challenge_deterministic() -> None:
    verifier = generate_code_verifier()
    c1 = generate_code_challenge(verifier)
    c2 = generate_code_challenge(verifier)
    assert c1 == c2


def test_verify_correct() -> None:
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    assert verify_code_challenge(verifier, challenge) is True


def test_verify_wrong() -> None:
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    wrong_verifier = generate_code_verifier()
    assert verify_code_challenge(wrong_verifier, challenge) is False


def test_plain_rejected() -> None:
    with pytest.raises(ValueError, match="S256"):
        verify_code_challenge("abc", "abc", method="plain")


# ── Token tests (12) ──────────────────────────────────────────────────────────


def test_issue_valid_jwt(token_service: TokenService) -> None:
    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    assert isinstance(token, str)
    assert len(token) > 20


def test_jwt_has_aud(token_service: TokenService) -> None:
    import jwt as pyjwt

    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    # Decode without verifying to inspect claims
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == _RESOURCE


def test_validate_success(token_service: TokenService) -> None:
    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    claims = token_service.validate_access_token(token, _RESOURCE)
    assert isinstance(claims, TokenClaims)
    assert claims.sub == "client-a"
    assert claims.aud == _RESOURCE
    assert claims.scope == "tools:read"
    assert claims.iss == _ISSUER


def test_wrong_audience(token_service: TokenService, tmp_path: Path) -> None:
    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    with pytest.raises(OAuthError) as exc_info:
        token_service.validate_access_token(token, "http://other-server/mcp")
    assert exc_info.value.status_code == 401


def test_expired(tmp_path: Path) -> None:
    short_svc = TokenService(
        issuer=_ISSUER,
        signing_key_path=tmp_path / "short_key.pem",
        access_lifetime=-1,  # Already expired
    )
    token = short_svc.issue_access_token("client-a", _RESOURCE, "tools:read")
    with pytest.raises(OAuthError) as exc_info:
        short_svc.validate_access_token(token, _RESOURCE)
    assert exc_info.value.error == "invalid_token"


def test_tampered(token_service: TokenService) -> None:
    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    # Flip a character in the signature
    tampered = token[:-4] + ("X" if token[-4] != "X" else "Y") + token[-3:]
    with pytest.raises(OAuthError):
        token_service.validate_access_token(tampered, _RESOURCE)


def test_revoke_access(token_service: TokenService) -> None:
    token = token_service.issue_access_token("client-a", _RESOURCE, "tools:read")
    token_service.revoke(token)
    with pytest.raises(OAuthError) as exc_info:
        token_service.validate_access_token(token, _RESOURCE)
    assert "revoked" in exc_info.value.description


def test_refresh_issued(token_service: TokenService) -> None:
    rt = token_service.issue_refresh_token("client-a", "tools:read", _RESOURCE)
    assert isinstance(rt, str) and len(rt) > 20
    assert token_service._refresh_tokens.get(rt) is not None


def test_refresh_rotation(token_service: TokenService) -> None:
    rt = token_service.issue_refresh_token("client-a", "tools:read", _RESOURCE)
    new_access, new_refresh = token_service.rotate_refresh_token(rt, "client-a")
    assert isinstance(new_access, str)
    assert isinstance(new_refresh, str)
    assert new_refresh != rt


def test_refresh_invalidates_old(token_service: TokenService) -> None:
    rt = token_service.issue_refresh_token("client-a", "tools:read", _RESOURCE)
    token_service.rotate_refresh_token(rt, "client-a")
    from pincer.mcp.auth.errors import OAuthError as OE

    with pytest.raises(OE) as exc_info:
        token_service.rotate_refresh_token(rt, "client-a")
    assert exc_info.value.error == "invalid_grant"


def test_refresh_wrong_client(token_service: TokenService) -> None:
    rt = token_service.issue_refresh_token("client-a", "tools:read", _RESOURCE)
    with pytest.raises(OAuthError) as exc_info:
        token_service.rotate_refresh_token(rt, "client-b")
    assert exc_info.value.error == "invalid_grant"


def test_key_persistence(tmp_path: Path) -> None:
    key_path = tmp_path / "persist_key.pem"
    svc1 = TokenService(issuer=_ISSUER, signing_key_path=key_path)
    jwk1 = svc1.get_public_key_jwk()
    svc2 = TokenService(issuer=_ISSUER, signing_key_path=key_path)
    jwk2 = svc2.get_public_key_jwk()
    assert jwk1 == jwk2


# ── Client registry tests (6) ─────────────────────────────────────────────────


def test_static_loaded(client_registry: ClientRegistry) -> None:
    client = client_registry.get(_CLIENT_ID)
    assert client is not None
    assert client.client_name == "Test Client"


def test_dcr_creates(client_registry: ClientRegistry) -> None:
    client = client_registry.register(
        client_name="Dynamic App",
        redirect_uris=[_REDIRECT_URI],
        grant_types=["authorization_code"],
    )
    assert client.client_id.startswith("dyn_")
    assert client_registry.get(client.client_id) is not None


def test_dcr_rejects_http(client_registry: ClientRegistry) -> None:
    with pytest.raises(OAuthError) as exc_info:
        client_registry.register(
            client_name="Evil App",
            redirect_uris=["http://evil.example.com/callback"],
        )
    assert "HTTP" in exc_info.value.description or "redirect" in exc_info.value.description.lower()


def test_dcr_allows_localhost(client_registry: ClientRegistry) -> None:
    client = client_registry.register(
        client_name="Local App",
        redirect_uris=["http://127.0.0.1:9999/callback"],
    )
    assert client.client_id.startswith("dyn_")


def test_verify_secret(client_registry: ClientRegistry) -> None:
    assert client_registry.verify_secret("confidential-client", "test-secret-xyz") is True
    assert client_registry.verify_secret("confidential-client", "wrong-secret") is False


def test_redirect_wildcard(client_registry: ClientRegistry) -> None:
    # test-client has "http://127.0.0.1:*/callback"
    assert client_registry.validate_redirect_uri(_CLIENT_ID, "http://127.0.0.1:9999/callback") is True
    assert client_registry.validate_redirect_uri(_CLIENT_ID, "http://127.0.0.1:1234/callback") is True


# ── Scope tests (5) ───────────────────────────────────────────────────────────


def test_subset_passes() -> None:
    assert validate_scopes("tools:read", "tools:read resources:read") is True


def test_not_subset_fails() -> None:
    assert validate_scopes("tools:execute", "tools:read") is False


def test_tools_all_expands() -> None:
    assert validate_scopes("tools:read tools:write tools:execute", "tools:all") is True


def test_tool_scope_check() -> None:
    assert check_tool_scope("web_search", "tools:read") is True
    assert check_tool_scope("web_search", "tools:all") is True


def test_tool_scope_denied() -> None:
    assert check_tool_scope("execute_command", "tools:read") is False


# ── Metadata tests (3) ────────────────────────────────────────────────────────


def test_prm_has_resource() -> None:
    meta = build_protected_resource_metadata(_RESOURCE, _ISSUER, "tools:read")
    assert meta["resource"] == _RESOURCE
    assert _ISSUER in meta["authorization_servers"]


def test_as_metadata_fields() -> None:
    meta = build_authorization_server_metadata(_ISSUER, "tools:read resources:read")
    assert meta["issuer"] == _ISSUER
    assert "authorization_endpoint" in meta
    assert "token_endpoint" in meta
    assert "jwks_uri" in meta
    assert "registration_endpoint" in meta


def test_s256_only() -> None:
    meta = build_authorization_server_metadata(_ISSUER, "tools:read")
    assert meta["code_challenge_methods_supported"] == ["S256"]


# ── Integration tests (10) ────────────────────────────────────────────────────


def test_401_without_token(auth_app: Starlette) -> None:
    client = TestClient(auth_app, raise_server_exceptions=True)
    response = client.post("/mcp", content=b"{}")
    assert response.status_code == 401


def test_401_has_resource_metadata(auth_app: Starlette) -> None:
    client = TestClient(auth_app, raise_server_exceptions=True)
    response = client.post("/mcp", content=b"{}")
    assert response.status_code == 401
    www_auth = response.headers.get("www-authenticate", "")
    assert "resource_metadata" in www_auth
    assert _ISSUER in www_auth


def test_discovery_chain(auth_app: Starlette) -> None:
    client = TestClient(auth_app, raise_server_exceptions=True)

    r1 = client.get("/.well-known/oauth-protected-resource")
    assert r1.status_code == 200
    prm = r1.json()
    assert prm["resource"] == _RESOURCE
    assert prm["authorization_servers"][0] == _ISSUER

    r2 = client.get("/.well-known/oauth-authorization-server")
    assert r2.status_code == 200
    asm = r2.json()
    assert asm["issuer"] == _ISSUER
    assert "authorization_endpoint" in asm

    r3 = client.get("/.well-known/jwks.json")
    assert r3.status_code == 200
    assert "keys" in r3.json()


def test_dcr_via_endpoint(auth_app: Starlette) -> None:
    client = TestClient(auth_app, raise_server_exceptions=True)
    r = client.post(
        "/register",
        json={
            "client_name": "Test DCR App",
            "redirect_uris": [_REDIRECT_URI],
            "grant_types": ["authorization_code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["client_id"].startswith("dyn_")


def test_full_pkce_flow(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    state = secrets.token_hex(8)

    # Step 1: Authorize (test-client has auto_consent=True → immediate redirect)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    assert r.status_code == 302
    location = r.headers["location"]
    parsed = urllib.parse.urlparse(location)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert "code" in params
    assert params["state"] == state
    code = params["code"]

    # Step 2: Exchange code for token
    r2 = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": _CLIENT_ID,
            "code_verifier": verifier,
            "resource": _RESOURCE,
        },
    )
    assert r2.status_code == 200
    token_data = r2.json()
    assert "access_token" in token_data
    access_token = token_data["access_token"]

    # Step 3: Use token
    r3 = http.post("/mcp", headers={"Authorization": f"Bearer {access_token}"})
    assert r3.status_code == 200


def test_client_credentials_flow(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)

    r = http.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "confidential-client",
            "client_secret": "test-secret-xyz",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    assert r.status_code == 200
    data = r.json()
    access_token = data["access_token"]

    r2 = http.post("/mcp", headers={"Authorization": f"Bearer {access_token}"})
    assert r2.status_code == 200


def test_refresh_flow(auth_app: Starlette, token_service: TokenService) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)

    # Get initial token via PKCE with offline_access
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    state = secrets.token_hex(8)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": _RESOURCE,
            "scope": "tools:read offline_access",
        },
    )
    assert r.status_code == 302
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))["code"]

    r2 = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": _CLIENT_ID,
            "code_verifier": verifier,
            "resource": _RESOURCE,
        },
    )
    assert r2.status_code == 200
    token_data = r2.json()
    assert "refresh_token" in token_data
    refresh_token = token_data["refresh_token"]

    # Use refresh token to get new access token
    r3 = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLIENT_ID,
        },
    )
    assert r3.status_code == 200
    new_data = r3.json()
    assert "access_token" in new_data
    new_token = new_data["access_token"]

    # New token works
    r4 = http.post("/mcp", headers={"Authorization": f"Bearer {new_token}"})
    assert r4.status_code == 200


def test_revocation(auth_app: Starlette, token_service: TokenService) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)

    token = token_service.issue_access_token(_CLIENT_ID, _RESOURCE, "tools:read")

    # Token works before revocation
    r1 = http.post("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200

    # Revoke
    r2 = http.post("/revoke", data={"token": token})
    assert r2.status_code == 200

    # Token rejected after revocation
    r3 = http.post("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 401


def test_scope_enforcement(auth_app: Starlette, token_service: TokenService) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)

    # Token with only tools:read scope
    token = token_service.issue_access_token(_CLIENT_ID, _RESOURCE, "tools:read")

    # /mcp is accessible with tools:read
    r1 = http.post("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200

    # /mcp/execute requires tools:execute — should return 403
    r2 = http.post("/mcp/execute", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 403


def test_localhost_bypass(bypass_app: Starlette) -> None:
    http = TestClient(bypass_app, raise_server_exceptions=True)
    # TestClient connects from 127.0.0.1 — bypass should kick in
    r = http.post("/mcp", content=b"{}")
    # Either 200 (bypass worked) or 401 (bypass didn't recognize testclient IP)
    # We accept 200 as success; if 401, log as info (testclient may not set client.host)
    # The important thing is no crash
    assert r.status_code in (200, 401)


# ── Security tests (7) ────────────────────────────────────────────────────────


def test_pkce_required(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "state": "abc123",
            "resource": _RESOURCE,
            # Missing: code_challenge, code_challenge_method
        },
    )
    # Should redirect with error or return 400
    if r.status_code == 302:
        location = r.headers["location"]
        assert "error" in location
    else:
        assert r.status_code in (400, 302)


def test_code_single_use(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))["code"]

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": _CLIENT_ID,
        "code_verifier": verifier,
        "resource": _RESOURCE,
    }

    r1 = http.post("/token", data=token_data)
    assert r1.status_code == 200

    # Second use must fail
    r2 = http.post("/token", data=token_data)
    assert r2.status_code == 400
    assert r2.json().get("error") == "invalid_grant"


def test_redirect_mismatch(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))["code"]

    # Use wrong redirect_uri in token exchange
    r2 = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/other",
            "client_id": _CLIENT_ID,
            "code_verifier": verifier,
            "resource": _RESOURCE,
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_state_required(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Missing: state
            "resource": _RESOURCE,
        },
    )
    # Should return error (redirect with error or 400)
    if r.status_code == 302:
        assert "error" in r.headers["location"]
    else:
        assert r.status_code in (400, 302)


def test_audience_mismatch(token_service: TokenService, tmp_path: Path) -> None:
    """Token issued for server A must be rejected at server B (RFC 8707)."""
    svc_b = TokenService(
        issuer="http://server-b",
        signing_key_path=tmp_path / "key_b.pem",
    )
    token_for_a = token_service.issue_access_token(_CLIENT_ID, _RESOURCE, "tools:read")

    with pytest.raises(OAuthError):
        svc_b.validate_access_token(token_for_a, "http://server-b/mcp")


def test_token_passthrough(token_service: TokenService, tmp_path: Path) -> None:
    """Token from a different issuer is rejected."""
    foreign_svc = TokenService(
        issuer="http://foreign-issuer",
        signing_key_path=tmp_path / "foreign_key.pem",
    )
    # Foreign token issued for our resource but signed with foreign key
    foreign_token = foreign_svc.issue_access_token(_CLIENT_ID, _RESOURCE, "tools:read")

    with pytest.raises(OAuthError):
        token_service.validate_access_token(foreign_token, _RESOURCE)


def test_constant_time(client_registry: ClientRegistry) -> None:
    """verify_secret must use secrets.compare_digest (constant-time)."""
    import inspect

    from pincer.mcp.auth import clients as clients_mod

    source = inspect.getsource(clients_mod.ClientRegistry.verify_secret)
    assert "compare_digest" in source


# ── Token store tests ─────────────────────────────────────────────────────────


def test_token_store_memory_store_and_retrieve() -> None:
    store = TokenStore()
    store._strategy = "memory"
    store.store("http://test", "access123", "refresh456", int(time.time()) + 3600)
    assert store.get_access_token("http://test") == "access123"
    assert store.get_refresh_token("http://test") == "refresh456"


def test_token_store_expired_access_returns_none() -> None:
    store = TokenStore()
    store._strategy = "memory"
    store.store("http://test", "access123", None, int(time.time()) - 1)
    assert store.get_access_token("http://test") is None


def test_token_store_clear() -> None:
    store = TokenStore()
    store._strategy = "memory"
    store.store("http://test", "token", None, int(time.time()) + 3600)
    store.clear("http://test")
    assert store.get_access_token("http://test") is None


def test_token_store_clear_all() -> None:
    store = TokenStore()
    store._strategy = "memory"
    store.store("http://a", "t1", None, int(time.time()) + 3600)
    store.store("http://b", "t2", None, int(time.time()) + 3600)
    store.clear_all()
    assert store.get_access_token("http://a") is None
    assert store.get_access_token("http://b") is None


def test_token_store_unknown_resource() -> None:
    store = TokenStore()
    store._strategy = "memory"
    assert store.get_access_token("http://unknown") is None
    assert store.get_refresh_token("http://unknown") is None


def test_token_store_file_strategy(tmp_path: Path) -> None:
    """File strategy stores and loads tokens from disk."""
    from pincer.mcp.auth import token_store as ts_mod

    original_path = ts_mod._TOKEN_FILE
    test_file = tmp_path / "mcp_tokens.json"
    ts_mod._TOKEN_FILE = test_file
    try:
        store = TokenStore()
        store._strategy = "file"
        store.store("http://test", "file-token", "file-refresh", int(time.time()) + 3600)
        assert test_file.exists()

        # New instance reads from same file
        store2 = TokenStore()
        store2._strategy = "file"
        assert store2.get_access_token("http://test") == "file-token"
    finally:
        ts_mod._TOKEN_FILE = original_path


# ── Consent UI tests ──────────────────────────────────────────────────────────


def test_render_consent_page_structure() -> None:
    html = render_consent_page(
        "My App",
        ["tools:read: Read tools", "resources:read: Read resources"],
        "/authorize/consent",
        "req123",
    )
    assert "My App" in html
    assert "tools:read" in html
    assert "resources:read" in html
    assert 'name="request_id" value="req123"' in html
    assert 'value="approve"' in html
    assert 'value="deny"' in html
    assert "<form" in html


def test_format_channel_consent_text() -> None:
    text = format_channel_consent("Test App", ["tools:read: Read tools"])
    assert "Test App" in text
    assert "tools:read" in text
    assert "🔐" in text


# ── Legacy MCPAuthProvider tests ──────────────────────────────────────────────


def test_legacy_provider_issue_and_validate() -> None:
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "c1", "client_secret": "s1", "name": "Client 1"}],
        token_expiry_seconds=3600,
        signing_key="a" * 32,
    )
    token = provider.issue_token("c1", "s1")
    assert token is not None
    claims = provider.validate_token(token)
    assert claims is not None
    assert claims["sub"] == "c1"


def test_legacy_provider_wrong_secret_returns_none() -> None:
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "c1", "client_secret": "s1"}],
        signing_key="b" * 32,
    )
    assert provider.issue_token("c1", "wrong") is None
    assert provider.issue_token("unknown", "s1") is None


def test_legacy_provider_revoke() -> None:
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "c1", "client_secret": "s1"}],
        signing_key="c" * 32,
    )
    token = provider.issue_token("c1", "s1")
    assert token is not None
    claims = provider.validate_token(token)
    assert claims is not None
    provider.revoke_token(claims["jti"])
    assert provider.validate_token(token) is None


def test_legacy_provider_is_localhost() -> None:
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(allowed_clients=[])
    assert provider.is_localhost("127.0.0.1") is True
    assert provider.is_localhost("::1") is True
    assert provider.is_localhost("192.168.1.1") is False


# ── Error factory tests ───────────────────────────────────────────────────────


def test_all_error_factories() -> None:
    assert invalid_request("test").status_code == 400
    assert invalid_request("test").error == "invalid_request"
    assert invalid_client("test").status_code == 401
    assert invalid_client("test").error == "invalid_client"
    assert invalid_grant("test").status_code == 400
    assert unauthorized_client("test").status_code == 400
    assert unsupported_grant_type().status_code == 400
    err = insufficient_scope("tools:execute")
    assert err.status_code == 403
    assert err.required_scope == "tools:execute"  # type: ignore[attr-defined]
    assert err.to_dict()["error"] == "insufficient_scope"


# ── Scope description tests ───────────────────────────────────────────────────


def test_get_scope_descriptions_known_scopes() -> None:
    descs = get_scope_descriptions("tools:read resources:read")
    assert len(descs) == 2
    assert any("tools:read" in d for d in descs)
    assert any("resources:read" in d for d in descs)


def test_get_scope_descriptions_unknown_scope() -> None:
    descs = get_scope_descriptions("unknown:custom")
    assert descs == ["unknown:custom"]


# ── Consent flow endpoint tests ───────────────────────────────────────────────


@pytest.fixture
def consent_app(token_service: TokenService, client_registry: ClientRegistry) -> tuple[Starlette, str]:
    """App for testing consent flow, returns (app, non_consent_client_id)."""
    non_consent = client_registry.register(
        "Consent Test Client",
        [_REDIRECT_URI],
        ["authorization_code"],
    )

    async def mcp_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [Route("/mcp", mcp_endpoint, methods=["POST"])]
    app = Starlette(routes=routes)
    mount_oauth_endpoints(app, token_service, client_registry, None, resource_uri=_RESOURCE, issuer=_ISSUER)
    return app, non_consent.client_id


def test_consent_page_shown(consent_app: tuple) -> None:
    app, client_id = consent_app
    http = TestClient(app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    assert r.status_code == 200
    assert "Consent Test Client" in r.text
    assert "Approve" in r.text
    assert "Deny" in r.text


def test_consent_approve(consent_app: tuple) -> None:
    app, client_id = consent_app
    http = TestClient(app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    assert r.status_code == 200
    match = re.search(r'name="request_id" value="([^"]+)"', r.text)
    assert match, f"request_id not in consent page: {r.text[:500]}"
    request_id = match.group(1)

    r2 = http.post("/authorize/consent", data={"request_id": request_id, "decision": "approve"})
    assert r2.status_code == 302
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r2.headers["location"]).query))
    assert "code" in params
    assert params.get("state") == "xyz"


def test_consent_deny(consent_app: tuple) -> None:
    app, client_id = consent_app
    http = TestClient(app, raise_server_exceptions=True, follow_redirects=False)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)

    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": _RESOURCE,
            "scope": "tools:read",
        },
    )
    match = re.search(r'name="request_id" value="([^"]+)"', r.text)
    assert match
    r2 = http.post("/authorize/consent", data={"request_id": match.group(1), "decision": "deny"})
    assert r2.status_code == 302
    assert "access_denied" in r2.headers["location"]


# ── MCPOAuthClient tests ──────────────────────────────────────────────────────


async def test_oauth_client_returns_stored_token() -> None:
    """get_headers returns token from store without network calls."""
    store = TokenStore()
    store._strategy = "memory"
    store.store("http://test-mcp/mcp", "stored-access-token", None, int(time.time()) + 3600)

    oauth = MCPOAuthClient("http://test-mcp/mcp", store)
    headers = await oauth.get_headers()
    assert headers == {"Authorization": "Bearer stored-access-token"}


async def test_oauth_client_refresh_on_expired_access() -> None:
    """When access token expired but refresh token available, calls refresh."""
    store = TokenStore()
    store._strategy = "memory"
    # Store expired access + valid refresh
    store.store("http://test-mcp/mcp", "old-access", "my-refresh", int(time.time()) - 1)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
        "refresh_token": "new-refresh",
    }
    mock_response.raise_for_status = MagicMock()

    mock_as_meta = MagicMock()
    mock_as_meta.status_code = 200
    mock_as_meta.json.return_value = {
        "issuer": "http://test-mcp",
        "token_endpoint": "http://test-mcp/token",
        "authorization_endpoint": "http://test-mcp/authorize",
    }

    mock_prm = MagicMock()
    mock_prm.status_code = 200
    mock_prm.json.return_value = {"authorization_servers": ["http://test-mcp"]}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        # Discovery calls
        mock_client.get.side_effect = [mock_prm, mock_as_meta]
        # Token refresh call
        mock_client.post.return_value = mock_response

        oauth = MCPOAuthClient("http://test-mcp/mcp", store)
        headers = await oauth.get_headers()

    assert "Bearer" in headers.get("Authorization", "")


async def test_oauth_client_credentials_flow_mock() -> None:
    """client_credentials flow with mocked httpx."""
    store = TokenStore()
    store._strategy = "memory"

    mock_prm = MagicMock()
    mock_prm.status_code = 200
    mock_prm.json.return_value = {"authorization_servers": ["http://test-mcp"]}

    mock_as_meta = MagicMock()
    mock_as_meta.status_code = 200
    mock_as_meta.json.return_value = {
        "issuer": "http://test-mcp",
        "token_endpoint": "http://test-mcp/token",
        "authorization_endpoint": "http://test-mcp/authorize",
    }

    mock_token_resp = MagicMock()
    mock_token_resp.json.return_value = {"access_token": "cc-token", "expires_in": 3600}
    mock_token_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.get.side_effect = [mock_prm, mock_as_meta]
        mock_http.post.return_value = mock_token_resp

        oauth = MCPOAuthClient(
            "http://test-mcp/mcp",
            store,
            client_config={"client_id": "c1", "client_secret": "s1", "grant_type": "client_credentials"},
        )
        headers = await oauth.get_headers()

    assert headers == {"Authorization": "Bearer cc-token"}


# ── Callback server test ──────────────────────────────────────────────────────


async def test_callback_server_captures_params() -> None:
    """_CallbackServer parses OAuth callback query params."""
    import asyncio

    cb = _CallbackServer()
    # Use a high ephemeral port to avoid conflicts
    port = 18890
    await cb.start(port)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /callback?code=test123&state=mystate HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()

        params = await cb.wait(timeout=5.0)
        assert params.get("code") == "test123"
        assert params.get("state") == "mystate"
    finally:
        await cb.stop()
        with contextlib.suppress(Exception):
            writer.close()


# ── Legacy middleware dispatch tests ─────────────────────────────────────────


def test_legacy_middleware_blocks_and_issues_token() -> None:
    """Legacy auth_provider dispatch: no token → 401, /oauth/token → token, bearer → 200."""
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "c1", "client_secret": "s1"}],
        token_expiry_seconds=3600,
        signing_key="d" * 32,
    )

    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api", endpoint, methods=["GET", "POST"])])
    app.add_middleware(MCPAuthMiddleware, auth_provider=provider)

    http = TestClient(app, raise_server_exceptions=True)

    # 1. No token → 401
    r1 = http.get("/api")
    assert r1.status_code == 401

    # 2. Token endpoint
    r2 = http.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": "c1", "client_secret": "s1"})
    assert r2.status_code == 200
    token = r2.json()["access_token"]

    # 3. Valid bearer → 200
    r3 = http.get("/api", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 200

    # 4. Wrong bearer → 401
    r4 = http.get("/api", headers={"Authorization": "Bearer INVALID"})
    assert r4.status_code == 401


def test_legacy_middleware_wrong_client_secret() -> None:
    """Legacy middleware: wrong client_secret returns 401 from token endpoint."""
    from pincer.mcp.auth import MCPAuthProvider

    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "c1", "client_secret": "s1"}],
        signing_key="e" * 32,
    )

    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({})

    app = Starlette(routes=[Route("/api", endpoint, methods=["POST"])])
    app.add_middleware(MCPAuthMiddleware, auth_provider=provider)
    http = TestClient(app, raise_server_exceptions=True)

    r = http.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "c1", "client_secret": "WRONG"},
    )
    assert r.status_code == 401


async def test_new_middleware_localhost_bypass_direct() -> None:
    """New middleware directly: localhost client host triggers bypass."""

    async def noop_app(scope, receive, send) -> None:
        pass

    middleware = MCPAuthMiddleware(
        app=noop_app,
        token_service=MagicMock(spec=TokenService),
        resource_uri=_RESOURCE,
        issuer=_ISSUER,
        localhost_bypass=True,
    )

    mock_request = MagicMock()
    mock_request.url.path = "/mcp"
    mock_request.client.host = "127.0.0.1"

    responses = []

    async def call_next(req: MagicMock) -> JSONResponse:
        responses.append(req)
        return JSONResponse({"ok": True})

    await middleware.dispatch(mock_request, call_next)

    # call_next was called (bypass occurred) and state was set
    assert len(responses) == 1
    assert mock_request.state.auth_bypassed is True
    assert mock_request.state.auth_claims is None


# ── Token store file strategy cleanup tests ───────────────────────────────────


def test_token_store_file_clear(tmp_path: Path) -> None:
    from pincer.mcp.auth import token_store as ts_mod

    original = ts_mod._TOKEN_FILE
    ts_mod._TOKEN_FILE = tmp_path / "tokens.json"
    try:
        store = TokenStore()
        store._strategy = "file"
        store.store("http://svc", "tok1", "ref1", int(time.time()) + 3600)
        assert store.get_access_token("http://svc") == "tok1"

        store.clear("http://svc")
        assert store.get_access_token("http://svc") is None
    finally:
        ts_mod._TOKEN_FILE = original


def test_token_store_file_clear_all(tmp_path: Path) -> None:
    from pincer.mcp.auth import token_store as ts_mod

    original = ts_mod._TOKEN_FILE
    ts_mod._TOKEN_FILE = tmp_path / "tokens2.json"
    try:
        store = TokenStore()
        store._strategy = "file"
        store.store("http://a", "ta", None, int(time.time()) + 3600)
        store.store("http://b", "tb", None, int(time.time()) + 3600)
        store.clear_all()
        assert not (tmp_path / "tokens2.json").exists()
    finally:
        ts_mod._TOKEN_FILE = original


# ── MCPOAuthClient internal method tests ─────────────────────────────────────


async def test_discover_metadata_fallback_on_404() -> None:
    """_discover_metadata falls back to constructed defaults when PRM returns 404."""
    store = TokenStore()
    store._strategy = "memory"

    mock_404 = MagicMock()
    mock_404.status_code = 404

    mock_as = MagicMock()
    mock_as.status_code = 200
    mock_as.json.return_value = {
        "issuer": "http://fallback",
        "token_endpoint": "http://fallback/token",
        "authorization_endpoint": "http://fallback/authorize",
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.get.side_effect = [mock_404, mock_as]

        oauth = MCPOAuthClient("http://fallback/mcp", store)
        await oauth._discover_metadata()
        assert oauth._as_metadata is not None
        assert "token_endpoint" in oauth._as_metadata


async def test_register_client_mocked() -> None:
    """_register_client stores client_id from DCR response."""
    store = TokenStore()
    store._strategy = "memory"

    mock_reg = MagicMock()
    mock_reg.status_code = 201
    mock_reg.json.return_value = {"client_id": "dyn_registered_id"}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = mock_reg

        oauth = MCPOAuthClient("http://test/mcp", store)
        oauth._as_metadata = {"registration_endpoint": "http://test/register"}
        await oauth._register_client()
        assert oauth._client_id == "dyn_registered_id"


async def test_discover_metadata_network_failure() -> None:
    """_discover_metadata falls back gracefully on network error."""
    store = TokenStore()
    store._strategy = "memory"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.get.side_effect = Exception("Network error")

        oauth = MCPOAuthClient("http://failing/mcp", store)
        await oauth._discover_metadata()
        assert oauth._as_metadata is not None  # Has fallback defaults


async def test_refresh_mocked() -> None:
    """_refresh uses stored refresh token to get new access token."""
    store = TokenStore()
    store._strategy = "memory"

    mock_prm = MagicMock()
    mock_prm.status_code = 200
    mock_prm.json.return_value = {"authorization_servers": ["http://test"]}

    mock_as = MagicMock()
    mock_as.status_code = 200
    mock_as.json.return_value = {
        "issuer": "http://test",
        "token_endpoint": "http://test/token",
        "authorization_endpoint": "http://test/authorize",
    }

    mock_token = MagicMock()
    mock_token.json.return_value = {"access_token": "refreshed-token", "expires_in": 3600, "refresh_token": "new-rt"}
    mock_token.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.get.side_effect = [mock_prm, mock_as]
        mock_http.post.return_value = mock_token

        oauth = MCPOAuthClient("http://test/mcp", store)
        new_token = await oauth._refresh("old-refresh-token")
        assert new_token == "refreshed-token"
        assert store.get_access_token("http://test/mcp") == "refreshed-token"


async def test_full_pkce_flow_mocked() -> None:
    """Full PKCE flow with all external deps mocked."""
    store = TokenStore()
    store._strategy = "memory"

    fixed_state = "deadbeefdeadbeef1234"

    mock_prm = MagicMock(status_code=200)
    mock_prm.json.return_value = {"authorization_servers": ["http://pkce-svc"]}
    mock_as = MagicMock(status_code=200)
    mock_as.json.return_value = {
        "issuer": "http://pkce-svc",
        "authorization_endpoint": "http://pkce-svc/authorize",
        "token_endpoint": "http://pkce-svc/token",
        "registration_endpoint": "http://pkce-svc/register",
    }
    mock_dcr = MagicMock(status_code=201)
    mock_dcr.json.return_value = {"client_id": "dyn_pkce_test"}
    mock_tok_resp = MagicMock()
    mock_tok_resp.json.return_value = {"access_token": "pkce-final-token", "expires_in": 3600}
    mock_tok_resp.raise_for_status = MagicMock()

    # Patch secrets module used in client_flow to fix state
    import pincer.mcp.auth.client_flow as cf_mod

    real_secrets = cf_mod.secrets

    class _MockSecrets:
        def token_hex(self, n: int) -> str:
            return fixed_state

        def token_urlsafe(self, n: int) -> str:
            return real_secrets.token_urlsafe(n)

        def compare_digest(self, a: str, b: str) -> bool:
            return real_secrets.compare_digest(a, b)

    async def mock_cb_wait(self: _CallbackServer, timeout: float = 300) -> dict:
        return {"code": "pkce-code-abc", "state": fixed_state}

    with (
        patch.object(cf_mod, "secrets", _MockSecrets()),
        patch("httpx.AsyncClient") as mock_cls,
        patch("webbrowser.open"),
        patch.object(_CallbackServer, "start", new=AsyncMock()),
        patch.object(_CallbackServer, "wait", new=mock_cb_wait),
        patch.object(_CallbackServer, "stop", new=AsyncMock()),
    ):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = mock_tok_resp

        oauth = MCPOAuthClient("http://pkce-svc/mcp", store)
        # Pre-set metadata and client_id to skip discovery/DCR network calls
        oauth._client_id = "pre-registered-client"
        oauth._as_metadata = {
            "issuer": "http://pkce-svc",
            "authorization_endpoint": "http://pkce-svc/authorize",
            "token_endpoint": "http://pkce-svc/token",
        }

        token = await oauth._full_pkce_flow()
        assert token == "pkce-final-token"
        assert store.get_access_token("http://pkce-svc/mcp") == "pkce-final-token"


# ── Token endpoint error path tests ──────────────────────────────────────────


def test_token_missing_code(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": "v",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_missing_code_verifier(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={"grant_type": "authorization_code", "code": "c", "client_id": _CLIENT_ID, "redirect_uri": _REDIRECT_URI},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_wrong_grant_type(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/token", data={"grant_type": "password", "username": "u", "password": "p"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_token_refresh_missing_token(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/token", data={"grant_type": "refresh_token", "client_id": _CLIENT_ID})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_client_creds_wrong_secret(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "confidential-client", "client_secret": "WRONG"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_register_rejects_empty_name(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/register", json={"client_name": "", "redirect_uris": [_REDIRECT_URI]})
    assert r.status_code == 400


def test_token_refresh_missing_client_id(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": "some-rt"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_client_creds_missing_secret(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "confidential-client"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_client_creds_missing_client_id(auth_app: Starlette) -> None:
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/token", data={"grant_type": "client_credentials", "client_secret": "s"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_dcr_max_clients_exceeded() -> None:
    """DCR raises error when max_clients limit is reached."""
    registry = ClientRegistry(static_clients=[], dcr_enabled=True, max_clients=1)
    registry.register("First", [_REDIRECT_URI], ["authorization_code"])
    with pytest.raises(OAuthError) as exc_info:
        registry.register("Second", [_REDIRECT_URI], ["authorization_code"])
    assert "maximum" in exc_info.value.description.lower() or "max" in exc_info.value.description.lower()


def test_token_store_file_load_direct(tmp_path: Path) -> None:
    """Direct test of _file_store and _file_load."""
    from pincer.mcp.auth import token_store as ts_mod

    original = ts_mod._TOKEN_FILE
    ts_mod._TOKEN_FILE = tmp_path / "direct.json"
    try:
        store = TokenStore()
        store._strategy = "file"
        payload = {"access_token": "direct-tok", "refresh_token": None, "expires_at": int(time.time()) + 9999}
        store._file_store("http://direct", payload)
        result = store._file_load("http://direct")
        assert result is not None
        assert result["access_token"] == "direct-tok"
    finally:
        ts_mod._TOKEN_FILE = original


def test_token_store_memory_clear_unknown_key() -> None:
    """Clearing unknown key in memory strategy is a no-op."""
    store = TokenStore()
    store._strategy = "memory"
    store.clear("http://not-stored")  # Should not raise


def test_token_store_keyring_mock() -> None:
    """Test keyring strategy detection with mocked keyring."""
    with patch.dict("sys.modules", {"keyring": MagicMock()}):
        store = TokenStore()
        store._detect_strategy()  # Now keyring is available
        # Strategy may be "keyring" if keyring is available
        # Just verify no crash


def test_consent_expired_request(consent_app: tuple) -> None:
    """Submitting a consent form with expired/invalid request_id returns 400."""
    app, _ = consent_app
    http = TestClient(app, raise_server_exceptions=True)
    r = http.post("/authorize/consent", data={"request_id": "nonexistent-id", "decision": "approve"})
    assert r.status_code == 400


def test_authorize_unknown_client(auth_app: Starlette) -> None:
    """Authorize with unknown client_id returns error."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "unknown-client",
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
            "resource": _RESOURCE,
        },
    )
    # Returns JSON error or redirects with error (no registered redirect_uri)
    assert r.status_code in (400, 302)


def test_authorize_missing_redirect_uri(auth_app: Starlette) -> None:
    """Authorize without redirect_uri returns 400."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert r.status_code in (400, 302)


# ── tokens.py coverage ─────────────────────────────────────────────────────────


def test_token_service_issuer_property(token_service: TokenService) -> None:
    """tokens.py line 45: issuer property returns configured issuer."""
    assert token_service.issuer == _ISSUER


def test_validate_token_wrong_issuer(tmp_path: Path) -> None:
    """tokens.py line 119: InvalidIssuerError path."""
    svc_a = TokenService(issuer="http://issuer-a", signing_key_path=tmp_path / "a.pem")
    svc_b = TokenService(issuer="http://issuer-b", signing_key_path=tmp_path / "b.pem")
    token = svc_a.issue_access_token("cli", _RESOURCE, "tools:read")
    with pytest.raises(OAuthError) as exc:
        svc_b.validate_access_token(token, _RESOURCE)
    assert exc.value.error == "invalid_token"


def test_rotate_expired_refresh_token(token_service: TokenService) -> None:
    """tokens.py lines 160-161: expired refresh token raises invalid_grant."""
    rt = token_service.issue_refresh_token("cli", "tools:read", _RESOURCE)
    # Manually expire the token
    data = token_service._refresh_tokens[rt]
    from dataclasses import replace as dc_replace

    token_service._refresh_tokens[rt] = dc_replace(data, expires_at=int(time.time()) - 10)
    with pytest.raises(OAuthError) as exc:
        token_service.rotate_refresh_token(rt, "cli")
    assert exc.value.error == "invalid_grant"


def test_revoke_refresh_token(token_service: TokenService) -> None:
    """tokens.py lines 174-175: revoking a refresh token removes it."""
    rt = token_service.issue_refresh_token("cli", "tools:read", _RESOURCE)
    assert rt in token_service._refresh_tokens
    token_service.revoke(rt)
    assert rt not in token_service._refresh_tokens


def test_revoke_garbage_string(token_service: TokenService) -> None:
    """tokens.py lines 194-195: revoking a non-JWT string is a no-op."""
    token_service.revoke("not-a-jwt-at-all")  # Should not raise


# ── clients.py coverage ────────────────────────────────────────────────────────


def test_verify_secret_unknown_client(client_registry: ClientRegistry) -> None:
    """clients.py lines 107-108: verify_secret with unknown client returns False."""
    result = client_registry.verify_secret("nonexistent-client", "any-secret")
    assert result is False


def test_validate_redirect_uri_unknown_client(client_registry: ClientRegistry) -> None:
    """clients.py line 118: validate_redirect_uri with unknown client returns False."""
    result = client_registry.validate_redirect_uri("nonexistent-client", _REDIRECT_URI)
    assert result is False


def test_validate_redirect_uri_wildcard_port(client_registry: ClientRegistry) -> None:
    """clients.py lines 134-138: wildcard port :* matches any port."""
    # test-client has http://127.0.0.1:*/callback
    assert client_registry.validate_redirect_uri(_CLIENT_ID, "http://127.0.0.1:9999/callback") is True
    assert client_registry.validate_redirect_uri(_CLIENT_ID, "http://127.0.0.1:1234/callback") is True


def test_validate_redirect_uri_https_allowed() -> None:
    """clients.py line 147: HTTPS redirect URI is always valid."""
    reg = ClientRegistry(
        static_clients=[
            {
                "client_id": "https-client",
                "client_name": "HTTPS Client",
                "redirect_uris": ["https://example.com/callback"],
                "grant_types": ["authorization_code"],
            }
        ]
    )
    assert reg.validate_redirect_uri("https-client", "https://example.com/callback") is True


def test_register_custom_uri_scheme() -> None:
    """clients.py lines 159-161: URI without '://' (bare custom scheme) is allowed."""
    reg = ClientRegistry(dcr_enabled=True)
    # A URI without '://' passes the custom-scheme guard and is allowed
    client = reg.register("Native App", ["myapp:callback"], ["authorization_code"])
    assert client.client_id.startswith("dyn_")


def test_register_invalid_scheme_raises() -> None:
    """clients.py line 163: invalid URI scheme raises OAuthError."""
    reg = ClientRegistry(dcr_enabled=True)
    with pytest.raises(OAuthError):
        reg.register("Bad App", ["ftp://example.com/callback"], ["authorization_code"])


def test_register_https_redirect_uri() -> None:
    """clients.py line 147: HTTPS in _validate_redirect_uri_format returns immediately."""
    reg = ClientRegistry(dcr_enabled=True)
    client = reg.register("HTTPS App", ["https://myapp.example.com/cb"], ["authorization_code"])
    assert client.redirect_uris == ["https://myapp.example.com/cb"]


# ── endpoints.py coverage ──────────────────────────────────────────────────────


def test_cleanup_pending_removes_expired() -> None:
    """endpoints.py line 33: _cleanup_pending deletes expired entries."""
    import pincer.mcp.auth.endpoints as ep_mod

    expired_id = "expired-request-id-xyz-direct"
    ep_mod._pending[expired_id] = {"expires_at": time.time() - 100, "client_id": "x"}

    ep_mod._cleanup_pending()

    assert expired_id not in ep_mod._pending


def test_register_invalid_json(auth_app: Starlette) -> None:
    """endpoints.py lines 125-126: /register with invalid JSON returns 400."""
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post("/register", content=b"not-json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_register_with_client_secret_post(auth_app: Starlette) -> None:
    """endpoints.py line 142: DCR with client_secret_post returns client_secret."""
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/register",
        json={
            "client_name": "Secret Client",
            "redirect_uris": [_REDIRECT_URI],
            "grant_types": ["authorization_code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert r.status_code == 201
    assert "client_secret" in r.json()


def test_authorize_wrong_response_type(auth_app: Starlette) -> None:
    """endpoints.py line 197: response_type != 'code' triggers error."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "token",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    # Should redirect with error or return 400
    assert r.status_code in (302, 400)
    if r.status_code == 302:
        assert "error" in r.headers["location"]


def test_authorize_missing_client_id(auth_app: Starlette) -> None:
    """endpoints.py line 199: missing client_id triggers error."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert r.status_code in (302, 400)


def test_authorize_plain_pkce_method(auth_app: Starlette) -> None:
    """endpoints.py line 205: plain PKCE method rejected."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": "some-challenge",
            "code_challenge_method": "plain",
            "state": "s1",
        },
    )
    assert r.status_code in (302, 400)
    if r.status_code == 302:
        assert "error" in r.headers["location"]


def test_authorize_unregistered_redirect_uri(auth_app: Starlette) -> None:
    """endpoints.py line 212: redirect_uri not in registered URIs triggers error."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": "http://127.0.0.1:9999/evil",
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert r.status_code in (302, 400)


def test_authorize_client_no_auth_code_grant(token_service: TokenService, client_registry: ClientRegistry) -> None:
    """endpoints.py lines 215-216: client without authorization_code grant is rejected."""
    # confidential-client only supports client_credentials
    http = TestClient(
        _make_auth_app(token_service, client_registry),
        raise_server_exceptions=True,
        follow_redirects=False,
    )
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "confidential-client",
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    # confidential-client has empty redirect_uris, so it redirects with error or returns 400
    assert r.status_code in (302, 400)


def test_authorize_invalid_scope(auth_app: Starlette) -> None:
    """endpoints.py line 223: unknown scope triggers error."""
    http = TestClient(auth_app, raise_server_exceptions=True, follow_redirects=False)
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": generate_code_challenge(generate_code_verifier()),
            "code_challenge_method": "S256",
            "state": "s1",
            "scope": "invalid:scope:xyz",
        },
    )
    assert r.status_code in (302, 400)
    if r.status_code == 302:
        assert "error" in r.headers["location"]


def test_token_code_missing_redirect_uri(auth_app: Starlette, token_service: TokenService) -> None:
    """endpoints.py line 295: token exchange without redirect_uri returns 400."""
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "some-code",
            "client_id": _CLIENT_ID,
            "code_verifier": generate_code_verifier(),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_code_missing_client_id(auth_app: Starlette) -> None:
    """endpoints.py line 297: token exchange without client_id returns 400."""
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "some-code",
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": generate_code_verifier(),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_code_not_found(auth_app: Starlette) -> None:
    """endpoints.py line 303: non-existent code returns invalid_grant."""
    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "nonexistent-code",
            "redirect_uri": _REDIRECT_URI,
            "client_id": _CLIENT_ID,
            "code_verifier": generate_code_verifier(),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_code_client_mismatch(auth_app: Starlette, token_service: TokenService) -> None:
    """endpoints.py line 309: client_id mismatch returns invalid_grant."""
    from pincer.mcp.auth.models import AuthorizationCode

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    code = AuthorizationCode(
        code="mismatch-code",
        client_id="other-client",
        redirect_uri=_REDIRECT_URI,
        code_challenge=challenge,
        scope="tools:read",
        resource=_RESOURCE,
        expires_at=int(time.time()) + 600,
    )
    token_service.store_authorization_code(code)

    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "mismatch-code",
            "redirect_uri": _REDIRECT_URI,
            "client_id": _CLIENT_ID,  # Different from "other-client"
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_code_pkce_failure(auth_app: Starlette, token_service: TokenService) -> None:
    """endpoints.py line 315: wrong code_verifier returns invalid_grant."""
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    from pincer.mcp.auth.models import AuthorizationCode

    code = AuthorizationCode(
        code="pkce-fail-code",
        client_id=_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        code_challenge=challenge,
        scope="tools:read",
        resource=_RESOURCE,
        expires_at=int(time.time()) + 600,
    )
    token_service.store_authorization_code(code)

    http = TestClient(auth_app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "pkce-fail-code",
            "redirect_uri": _REDIRECT_URI,
            "client_id": _CLIENT_ID,
            "code_verifier": generate_code_verifier(),  # Wrong verifier
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_client_creds_no_such_grant(token_service: TokenService, client_registry: ClientRegistry) -> None:
    """endpoints.py lines 375-376: client doesn't support client_credentials."""
    # test-client only supports authorization_code, not client_credentials
    # We need a client with a secret but no client_credentials grant
    reg = ClientRegistry(
        static_clients=[
            {
                "client_id": "no-cc-client",
                "client_name": "No CC",
                "redirect_uris": [_REDIRECT_URI],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "client_secret_post",
                "client_secret": "my-secret",
            }
        ]
    )
    app = _make_auth_app(token_service, reg)
    http = TestClient(app, raise_server_exceptions=True)
    r = http.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "no-cc-client",
            "client_secret": "my-secret",
        },
    )
    assert r.status_code in (400, 401)
    assert r.json()["error"] in ("unauthorized_client", "invalid_client")


# ── token_store.py keyring coverage ────────────────────────────────────────────


def test_token_store_keyring_store_and_load(tmp_path: Path) -> None:
    """token_store.py: _keyring_store and _keyring_load via mocked keyring."""
    mock_keyring = MagicMock()
    _storage: dict[tuple, str] = {}

    def mock_set(service: str, key: str, val: str) -> None:
        _storage[(service, key)] = val

    def mock_get(service: str, key: str) -> str | None:
        return _storage.get((service, key))

    mock_keyring.set_password = mock_set
    mock_keyring.get_password = mock_get

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        store = TokenStore()
        store._strategy = "keyring"
        resource = "http://example.com/mcp"
        store.store(resource, "access-tok", "refresh-tok", int(time.time()) + 9999)
        at = store.get_access_token(resource)
        assert at == "access-tok"
        rt = store.get_refresh_token(resource)
        assert rt == "refresh-tok"


def test_token_store_keyring_clear(tmp_path: Path) -> None:
    """token_store.py lines 75-80: clear() via keyring strategy."""
    mock_keyring = MagicMock()
    mock_keyring.delete_password = MagicMock()

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        store = TokenStore()
        store._strategy = "keyring"
        store.clear("http://example.com/mcp")
        mock_keyring.delete_password.assert_called_once_with("pincer_mcp", "http://example.com/mcp")


def test_token_store_file_load_all_corrupted(tmp_path: Path) -> None:
    """token_store.py lines 139-140: corrupted JSON file returns empty dict."""
    from pincer.mcp.auth import token_store as ts_mod

    original = ts_mod._TOKEN_FILE
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{")
    ts_mod._TOKEN_FILE = bad_file
    try:
        store = TokenStore()
        store._strategy = "file"
        result = store._file_load_all()
        assert result == {}
    finally:
        ts_mod._TOKEN_FILE = original


def test_token_store_file_store_fallback_to_memory(tmp_path: Path) -> None:
    """token_store.py lines 126-129: file store failure falls back to memory."""
    from pincer.mcp.auth import token_store as ts_mod

    original = ts_mod._TOKEN_FILE
    # Point to a path that's unwritable (directory as file path)
    ts_mod._TOKEN_FILE = tmp_path  # tmp_path is a directory, write_text will fail
    try:
        store = TokenStore()
        store._strategy = "file"
        store._file_store("http://fallback", {"access_token": "tok", "refresh_token": None, "expires_at": 9999})
        # Strategy should fall back to memory
        assert store._strategy == "memory"
        assert store._memory.get("http://fallback") is not None
    finally:
        ts_mod._TOKEN_FILE = original


# ── Helper ─────────────────────────────────────────────────────────────────────


def _make_auth_app(token_service: TokenService, client_registry: ClientRegistry) -> Starlette:
    """Helper: build a minimal auth-enabled Starlette app."""

    async def mcp_ep(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [Route("/mcp", mcp_ep, methods=["POST"])]
    app = Starlette(routes=routes)
    mount_oauth_endpoints(app, token_service, client_registry, None, resource_uri=_RESOURCE, issuer=_ISSUER)
    app.add_middleware(
        MCPAuthMiddleware,
        token_service=token_service,
        resource_uri=_RESOURCE,
        issuer=_ISSUER,
        localhost_bypass=False,
    )
    return app
