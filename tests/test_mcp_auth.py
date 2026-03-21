"""Tests for MCPAuthProvider and MCPAuthMiddleware."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from pincer.mcp.auth import MCPAuthMiddleware, MCPAuthProvider

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_provider(**kwargs) -> MCPAuthProvider:
    clients = kwargs.pop("allowed_clients", [
        {"client_id": "claude-desktop", "client_secret": "secret123", "name": "Claude Desktop"},
    ])
    return MCPAuthProvider(allowed_clients=clients, signing_key="test-signing-key", **kwargs)


# ── Token issuance ─────────────────────────────────────────────────────────────


def test_issue_token_valid_client():
    provider = _make_provider()
    token = provider.issue_token("claude-desktop", "secret123")
    assert token is not None
    assert isinstance(token, str)
    assert len(token) > 20


def test_issue_token_unknown_client():
    provider = _make_provider()
    token = provider.issue_token("unknown-client", "secret123")
    assert token is None


def test_issue_token_wrong_secret():
    provider = _make_provider()
    token = provider.issue_token("claude-desktop", "wrong-secret")
    assert token is None


# ── Token validation ──────────────────────────────────────────────────────────


def test_validate_token_success():
    provider = _make_provider()
    token = provider.issue_token("claude-desktop", "secret123")
    assert token is not None
    claims = provider.validate_token(token)
    assert claims is not None
    assert claims["sub"] == "claude-desktop"
    assert claims["name"] == "Claude Desktop"


def test_validate_token_expired():
    provider = _make_provider(token_expiry_seconds=1)
    token = provider.issue_token("claude-desktop", "secret123")
    assert token is not None

    # Patch time to simulate expiry
    import jwt as pyjwt

    # Decode and re-encode with exp in the past
    expired_payload = {
        "sub": "claude-desktop",
        "name": "Claude Desktop",
        "iat": int(time.time()) - 7200,
        "exp": int(time.time()) - 3600,
        "jti": "expiredtoken",
    }
    expired_token = pyjwt.encode(expired_payload, "test-signing-key", algorithm="HS256")
    assert provider.validate_token(expired_token) is None


def test_validate_token_tampered():
    provider = _make_provider()
    token = provider.issue_token("claude-desktop", "secret123")
    assert token is not None
    # Tamper with the token
    tampered = token[:-5] + "XXXXX"
    assert provider.validate_token(tampered) is None


def test_revoke_token():
    provider = _make_provider()
    token = provider.issue_token("claude-desktop", "secret123")
    assert token is not None
    claims = provider.validate_token(token)
    assert claims is not None

    # Revoke the token by its JTI
    jti = claims["jti"]
    provider.revoke_token(jti)

    # Now validation should fail
    assert provider.validate_token(token) is None


# ── Localhost bypass ──────────────────────────────────────────────────────────


def test_localhost_bypass_ipv4():
    provider = _make_provider()
    assert provider.is_localhost("127.0.0.1") is True


def test_localhost_bypass_ipv6():
    provider = _make_provider()
    assert provider.is_localhost("::1") is True


def test_localhost_bypass_named():
    provider = _make_provider()
    assert provider.is_localhost("localhost") is True


def test_non_localhost_requires_token():
    provider = _make_provider()
    assert provider.is_localhost("192.168.1.100") is False
    assert provider.is_localhost("10.0.0.1") is False


def test_auto_generate_secret_if_not_provided():
    """A client without a pre-set secret gets one auto-generated."""
    provider = MCPAuthProvider(
        allowed_clients=[{"client_id": "auto-client"}],
        signing_key="test-key",
    )
    client = provider._clients["auto-client"]
    assert len(client.client_secret) > 10


# ── Middleware ────────────────────────────────────────────────────────────────


def _make_mock_request(host: str, path: str = "/mcp", method: str = "GET", headers: dict | None = None):
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = host
    request.url = MagicMock()
    request.url.path = path
    request.method = method
    request.headers = headers or {}
    return request


async def test_middleware_passes_localhost():
    provider = _make_provider()
    middleware = MCPAuthMiddleware(MagicMock(), auth_provider=provider)

    request = _make_mock_request("127.0.0.1")
    call_next = AsyncMock(return_value=MagicMock(status_code=200))

    await middleware.dispatch(request, call_next)
    call_next.assert_called_once_with(request)


async def test_middleware_blocks_without_token():
    provider = _make_provider()
    middleware = MCPAuthMiddleware(MagicMock(), auth_provider=provider)

    request = _make_mock_request("192.168.1.50", headers={})
    call_next = AsyncMock(return_value=MagicMock(status_code=200))

    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 401
    call_next.assert_not_called()


async def test_middleware_passes_valid_token():
    provider = _make_provider()
    middleware = MCPAuthMiddleware(MagicMock(), auth_provider=provider)

    token = provider.issue_token("claude-desktop", "secret123")
    request = _make_mock_request(
        "192.168.1.50",
        headers={"Authorization": f"Bearer {token}"},
    )
    call_next = AsyncMock(return_value=MagicMock(status_code=200))

    await middleware.dispatch(request, call_next)
    call_next.assert_called_once()
