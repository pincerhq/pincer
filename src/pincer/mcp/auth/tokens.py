"""JWT access token service with Ed25519 signing (RFC 9068 + RFC 8707)."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional, cast

from pincer.mcp.auth.errors import OAuthError, invalid_grant
from pincer.mcp.auth.models import AuthorizationCode, RefreshTokenData, TokenClaims

logger = logging.getLogger("pincer.mcp.auth.tokens")


class TokenService:
    """
    Issues, validates, and revokes JWT access tokens.

    Uses Ed25519 (EdDSA) signing. The private key is generated on first use
    and persisted to disk at signing_key_path with mode 0o600.
    """

    def __init__(
        self,
        issuer: str,
        signing_key_path: Path | str,
        access_lifetime: int = 3600,
        refresh_lifetime: int = 86400,
    ) -> None:
        self._issuer = issuer
        self._key_path = Path(signing_key_path)
        self._access_lifetime = access_lifetime
        self._refresh_lifetime = refresh_lifetime
        self._revoked_jtis: set[str] = set()
        self._refresh_tokens: dict[str, RefreshTokenData] = {}
        self._authorization_codes: dict[str, AuthorizationCode] = {}
        self._private_key, self._public_key = self._load_or_generate_keys()

    @property
    def issuer(self) -> str:
        return self._issuer

    # ── Key management ─────────────────────────────────────────────────────────

    def _load_or_generate_keys(self) -> tuple[Any, Any]:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                load_pem_private_key,
            )
        except ImportError as e:
            raise RuntimeError(
                "cryptography package required for MCP auth: uv pip install 'pincer-agent[mcp]'"
            ) from e

        if not self._key_path.exists():
            private_key = Ed25519PrivateKey.generate()
            pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            self._key_path.write_bytes(pem)
            self._key_path.chmod(0o600)
            logger.info("Generated new Ed25519 signing key at %s", self._key_path)
        else:
            pem = self._key_path.read_bytes()
            private_key = cast(Ed25519PrivateKey, load_pem_private_key(pem, password=None))

        return private_key, private_key.public_key()

    # ── Access tokens ──────────────────────────────────────────────────────────

    def issue_access_token(self, client_id: str, resource: str, scope: str) -> str:
        """Issue a signed JWT access token (RFC 9068)."""
        try:
            import jwt
        except ImportError as e:
            raise RuntimeError("PyJWT required for MCP auth: uv pip install 'pincer-agent[mcp]'") from e

        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "sub": client_id,
            "aud": resource,  # RFC 8707 resource indicator
            "exp": now + self._access_lifetime,
            "iat": now,
            "jti": secrets.token_hex(16),
            "scope": scope,
            "client_id": client_id,
        }
        token: str = jwt.encode(claims, self._private_key, algorithm="EdDSA")
        return token

    def validate_access_token(self, token: str, expected_resource: str) -> TokenClaims:
        """Validate a JWT and return its claims. Raises OAuthError on failure."""
        try:
            import jwt as pyjwt
        except ImportError as e:
            raise RuntimeError("PyJWT required") from e

        try:
            raw: dict[str, Any] = pyjwt.decode(
                token,
                self._public_key,
                algorithms=["EdDSA"],
                audience=expected_resource,
                issuer=self._issuer,
            )
        except pyjwt.ExpiredSignatureError:
            raise OAuthError("invalid_token", "Token has expired", 401) from None
        except pyjwt.InvalidAudienceError:
            raise OAuthError("invalid_token", "Invalid token audience", 401) from None
        except pyjwt.InvalidIssuerError:
            raise OAuthError("invalid_token", "Invalid token issuer", 401) from None
        except pyjwt.InvalidTokenError as e:
            raise OAuthError("invalid_token", f"Invalid token: {e}", 401) from None

        jti = raw.get("jti", "")
        if jti in self._revoked_jtis:
            raise OAuthError("invalid_token", "Token has been revoked", 401)

        return TokenClaims(
            iss=raw["iss"],
            sub=raw["sub"],
            aud=raw["aud"] if isinstance(raw["aud"], str) else raw["aud"][0],
            exp=raw["exp"],
            iat=raw["iat"],
            jti=jti,
            scope=raw.get("scope", ""),
            client_id=raw.get("client_id", raw["sub"]),
        )

    # ── Refresh tokens ─────────────────────────────────────────────────────────

    def issue_refresh_token(self, client_id: str, scope: str, resource: str) -> str:
        """Issue an opaque refresh token and store it."""
        token = secrets.token_urlsafe(48)
        self._refresh_tokens[token] = RefreshTokenData(
            token=token,
            client_id=client_id,
            scope=scope,
            resource=resource,
            expires_at=int(time.time()) + self._refresh_lifetime,
        )
        return token

    def rotate_refresh_token(self, old_token: str, client_id: str) -> tuple[str, str]:
        """Validate and rotate a refresh token. Returns (new_access, new_refresh)."""
        data = self._refresh_tokens.get(old_token)
        if not data:
            raise invalid_grant("Refresh token not found or already used")
        if data.client_id != client_id:
            raise invalid_grant("Client mismatch for refresh token")
        if data.expires_at < int(time.time()):
            del self._refresh_tokens[old_token]
            raise invalid_grant("Refresh token has expired")

        del self._refresh_tokens[old_token]
        new_access = self.issue_access_token(data.client_id, data.resource, data.scope)
        new_refresh = self.issue_refresh_token(data.client_id, data.scope, data.resource)
        return new_access, new_refresh

    # ── Revocation ─────────────────────────────────────────────────────────────

    def revoke(self, token: str) -> None:
        """Revoke a token (RFC 7009). Silently ignores unknown tokens."""
        # Try as opaque refresh token first
        if token in self._refresh_tokens:
            del self._refresh_tokens[token]
            return

        # Try to decode as JWT (without validation) to extract jti
        try:
            import jwt as pyjwt

            raw = pyjwt.decode(
                token,
                self._public_key,
                algorithms=["EdDSA"],
                options={
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
            jti = raw.get("jti")
            if jti:
                self._revoked_jtis.add(jti)
        except Exception:
            pass  # Not a valid JWT — nothing to revoke

    # ── JWK Set ────────────────────────────────────────────────────────────────

    def get_public_key_jwk(self) -> dict[str, str]:
        """Return the public key as an RFC 8037 JWK (OKP, Ed25519)."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        kid = hashlib.sha256(raw).hexdigest()[:16]
        return {"kty": "OKP", "crv": "Ed25519", "x": x, "use": "sig", "kid": kid}

    # ── Authorization codes ────────────────────────────────────────────────────

    def store_authorization_code(self, code: AuthorizationCode) -> None:
        self._authorization_codes[code.code] = code

    def get_authorization_code(self, code: str) -> Optional[AuthorizationCode]:
        return self._authorization_codes.get(code)
