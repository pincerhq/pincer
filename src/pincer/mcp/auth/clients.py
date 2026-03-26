"""OAuth client registry with dynamic client registration (RFC 7591)."""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any, Optional
from urllib.parse import urlparse

from pincer.mcp.auth.errors import invalid_request, unauthorized_client
from pincer.mcp.auth.models import OAuthClient

logger = logging.getLogger("pincer.mcp.auth.clients")

# Ports that are unconditionally allowed for redirect URIs
_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ClientRegistry:
    """
    Stores and manages OAuth clients.

    Static clients are loaded from config; dynamic clients are registered
    via DCR (RFC 7591) at runtime. All storage is in-memory.
    """

    def __init__(
        self,
        static_clients: list[dict[str, Any]] | None = None,
        dcr_enabled: bool = True,
        max_clients: int = 20,
    ) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._dcr_enabled = dcr_enabled
        self._max_clients = max_clients

        for raw in static_clients or []:
            client = OAuthClient(
                client_id=raw["client_id"],
                client_name=raw.get("client_name", raw["client_id"]),
                redirect_uris=raw.get("redirect_uris", []),
                grant_types=raw.get("grant_types", ["authorization_code"]),
                token_endpoint_auth_method=raw.get("token_endpoint_auth_method", "none"),
                client_secret=raw.get("client_secret"),
                auto_consent=raw.get("auto_consent", False),
                is_static=True,
            )
            self._clients[client.client_id] = client
            logger.debug("Loaded static client: %s", client.client_id)

    # ── Lookups ────────────────────────────────────────────────────────────────

    def get(self, client_id: str) -> Optional[OAuthClient]:
        return self._clients.get(client_id)

    # ── Dynamic Client Registration ────────────────────────────────────────────

    def register(
        self,
        client_name: str,
        redirect_uris: list[str],
        grant_types: list[str] | None = None,
        auth_method: str = "none",
    ) -> OAuthClient:
        """Register a new dynamic client. Raises OAuthError on invalid input."""
        if not self._dcr_enabled:
            raise unauthorized_client("Dynamic client registration is disabled")

        dynamic_count = sum(1 for c in self._clients.values() if not c.is_static)
        if dynamic_count >= self._max_clients:
            raise invalid_request(f"Maximum dynamic clients ({self._max_clients}) reached")

        if not client_name:
            raise invalid_request("client_name is required")

        if not redirect_uris:
            raise invalid_request("At least one redirect_uri is required")

        for uri in redirect_uris:
            self._validate_redirect_uri_format(uri)

        grant_types = grant_types or ["authorization_code"]
        client_secret = secrets.token_urlsafe(32) if auth_method == "client_secret_post" else None

        client = OAuthClient(
            client_id=f"dyn_{secrets.token_hex(16)}",
            client_name=client_name,
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            token_endpoint_auth_method=auth_method,
            client_secret=client_secret,
            auto_consent=False,
            is_static=False,
        )
        self._clients[client.client_id] = client
        logger.info("Registered dynamic client: %s (%s)", client.client_id, client_name)
        return client

    # ── Validation ─────────────────────────────────────────────────────────────

    def verify_secret(self, client_id: str, secret: str) -> bool:
        """Constant-time secret comparison. Returns False if client not found."""
        client = self._clients.get(client_id)
        if not client or not client.client_secret:
            # Constant-time comparison against a dummy value to prevent timing attacks
            secrets.compare_digest(secret, "dummy-constant-time-padding-value-xyz")
            return False
        return secrets.compare_digest(secret, client.client_secret)

    def validate_redirect_uri(self, client_id: str, uri: str) -> bool:
        """Check that uri is in the client's registered redirect_uris.

        Supports wildcard port: http://127.0.0.1:* matches any port.
        """
        client = self._clients.get(client_id)
        if not client:
            return False

        for registered in client.redirect_uris:
            if registered == uri:
                return True
            # Wildcard port matching: http://127.0.0.1:*/path matches any port
            if ":*" in registered:
                # Replace :* with a port pattern for comparison
                parsed_uri = urlparse(uri)
                # Build what the registered URI looks like with the actual port
                if parsed_uri.port is not None:
                    candidate = registered.replace(":*", f":{parsed_uri.port}", 1)
                    if candidate == uri:
                        return True
                    # Also try without port (port 80/443)
                else:
                    candidate = registered.replace(":*", "", 1)
                    if candidate == uri:
                        return True

        return False

    # ── Private helpers ────────────────────────────────────────────────────────

    def _validate_redirect_uri_format(self, uri: str) -> None:
        """Reject URIs that are not localhost or HTTPS."""
        parsed = urlparse(uri)

        if parsed.scheme == "https":
            return  # HTTPS always OK

        if parsed.scheme == "http":
            host = parsed.hostname or ""
            # Allow localhost and loopback
            if host in _LOCALHOST_HOSTS or re.match(r"^127\.\d+\.\d+\.\d+$", host):
                return
            raise invalid_request(
                f"Non-localhost HTTP redirect URI not allowed: {uri!r}. "
                "Use HTTPS or a localhost URI."
            )

        if "://" not in uri:
            # Custom URI scheme (e.g. myapp://callback) — allowed for native apps
            return

        raise invalid_request(f"Invalid redirect URI scheme: {parsed.scheme!r}")
