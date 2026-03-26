"""PKCE (RFC 7636) S256 code challenge helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets


def generate_code_verifier() -> str:
    """Generate a cryptographically random PKCE code verifier (43–128 chars)."""
    return secrets.token_urlsafe(64)[:96]


def generate_code_challenge(verifier: str) -> str:
    """Compute S256 code challenge from verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_code_challenge(verifier: str, challenge: str, method: str = "S256") -> bool:
    """Verify that verifier produces challenge. Only S256 is supported."""
    if method != "S256":
        raise ValueError(f"Unsupported PKCE method: {method!r}. Only S256 is supported.")
    expected = generate_code_challenge(verifier)
    return secrets.compare_digest(expected, challenge)
