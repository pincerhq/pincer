"""
Microsoft 365 OAuth authentication using MSAL.

Supports:
1. Device code flow (headless — user enters code in browser)
2. Interactive browser flow (opens browser automatically)

Token cache: ~/.pincer/ms365_token_cache.json
MSAL handles refresh tokens automatically.

Run ``ms365-mcp-setup`` to perform the one-time device code auth flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Scopes grouped by service.
SERVICE_SCOPES: dict[str, list[str]] = {
    "email": ["Mail.ReadWrite", "Mail.Send"],
    "calendar": ["Calendars.ReadWrite"],
    "onedrive": ["Files.ReadWrite.All"],
    "todo": ["Tasks.ReadWrite"],
    "contacts": ["Contacts.ReadWrite"],
    "onenote": ["Notes.ReadWrite.All"],
}


def scopes_for_services(services: list[str] | None = None) -> list[str]:
    """Return the deduplicated scope list for the given service names."""
    if services is None:
        return ["User.Read"]+[scope for scopes in SERVICE_SCOPES.values()
            for scope in scopes]
    seen: set[str] = set()
    result: list[str] = ["User.Read"]
    seen.add("User.Read")
    for svc in services:
        for scope in SERVICE_SCOPES.get(svc, []):
            if scope not in seen:
                seen.add(scope)
                result.append(scope)
    return result


class MS365AuthError(Exception):
    """Raised when authentication fails."""


class MS365Auth:
    """
    Manages Microsoft 365 OAuth credentials via MSAL.

    Loads an existing token cache and refreshes automatically.
    When no valid cached token exists, provides device code or interactive flow.
    """

    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        cache_path: str = "~/.pincer/ms365_token_cache.json",
        services: list[str] | None = None,
    ) -> None:
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._cache_path = Path(os.path.expanduser(cache_path))
        self._scopes = scopes_for_services(services)
        self._app: Any = None  # msal.PublicClientApplication
        self._cache: Any = None  # msal.SerializableTokenCache
        self._pending_flow_message: str = ""

    def _ensure_app(self) -> None:
        """Lazily initialise the MSAL application."""
        if self._app is not None:
            return

        import msal

        self._cache = msal.SerializableTokenCache()
        self._load_cache()

        self._app = msal.PublicClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            token_cache=self._cache,
        )

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            self._cache.deserialize(self._cache_path.read_text())

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(self._cache.serialize())
            os.chmod(self._cache_path, 0o600)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_token_silent(self) -> str | None:
        """Try to acquire a token silently from cache. Returns None if not possible."""
        self._ensure_app()
        accounts = self._app.get_accounts()
        if not accounts:
            return None
        result = self._app.acquire_token_silent(self._scopes, account=accounts[0])
        if result and "access_token" in result:
            self._save_cache()
            return str(result["access_token"])
        return None

    async def get_token(self) -> str:
        """Get valid access token. Uses cache/refresh first, then raises if no cached token."""
        self._ensure_app()

        # Try silent (cached/refresh token)
        token = await asyncio.to_thread(self.get_token_silent)
        if token:
            return token

        raise MS365AuthError("No valid Microsoft 365 token found. Run 'ms365-mcp-setup' to authenticate.")

    def device_code_flow_sync(self) -> dict[str, Any]:
        """Run device code flow synchronously (blocking).

        Call this BEFORE starting an asyncio event loop so that all MSAL calls
        execute in the same thread with no asyncio/threading indirection.
        stdout is reserved for the MCP stdio protocol; the user-code message is
        printed to stderr.
        """
        import time

        self._ensure_app()

        flow = self._app.initiate_device_flow(self._scopes)
        if "user_code" not in flow:
            raise MS365AuthError(
                f"Device flow failed: {flow.get('error_description', 'unknown error')}"
            )

        msg = flow.get(
            "message",
            f"To sign in, visit https://microsoft.com/devicelogin and enter code: {flow['user_code']}",
        )
        self._pending_flow_message = msg
        print(f"\n{msg}\n", file=sys.stderr, flush=True)

        expires_in = flow.get("expires_in", 900)
        expires_at = flow.get("expires_at", time.time() + expires_in)
        logger.debug(
            "Device flow: expires_in=%s expires_at=%.0f now=%.0f remaining=%.0fs",
            expires_in,
            expires_at,
            time.time(),
            expires_at - time.time(),
        )

        result = dict(self._app.acquire_token_by_device_flow(flow))  # type: ignore[no-any-return]

        if "access_token" not in result:
            error_code = result.get("error", "")
            error_desc = result.get("error_description", "unknown error")
            logger.error(
                "Device code flow failed — error=%s desc=%s full_result=%s",
                error_code,
                error_desc,
                result,
            )
            raise MS365AuthError(f"Auth failed ({error_code}): {error_desc}")

        self._save_cache()
        return result

    async def device_code_flow(self) -> dict[str, Any]:
        """Async wrapper around device_code_flow_sync for programmatic / test use."""
        return await asyncio.to_thread(self.device_code_flow_sync)

    async def interactive_flow(self) -> dict[str, Any]:
        """Run interactive browser flow. Returns MSAL result."""
        self._ensure_app()

        result = await asyncio.to_thread(
            self._app.acquire_token_interactive,
            self._scopes,
        )

        if "access_token" not in result:
            raise MS365AuthError(f"Auth failed: {result.get('error_description', 'unknown error')}")

        self._save_cache()
        return dict(result)

    def has_cached_token(self) -> bool:
        """Return True only if a silent token acquisition actually succeeds.

        Checking for cached accounts alone is insufficient — the account may
        exist but its refresh token could be expired or revoked, which would
        cause get_token() to fail silently after skipping the device flow.
        """
        self._ensure_app()
        accounts = self._app.get_accounts()
        if not accounts:
            return False
        result = self._app.acquire_token_silent(self._scopes, account=accounts[0])
        has_token = bool(result and "access_token" in result)
        if has_token:
            self._save_cache()
        else:
            logger.debug("has_cached_token: silent refresh failed — %s", result)
        return has_token

    def authenticated_account(self) -> str | None:
        """Return the username from cached accounts, if any."""
        self._ensure_app()
        accounts = self._app.get_accounts()
        if accounts:
            username = accounts[0].get("username")
            return str(username) if username is not None else None
        return None

    @property
    def scopes(self) -> list[str]:
        return list(self._scopes)

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def cache_path(self) -> Path:
        return self._cache_path
