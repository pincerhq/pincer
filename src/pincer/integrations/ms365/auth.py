"""
Microsoft 365 OAuth authentication using MSAL.

Supports:
1. Device code flow (headless — user enters code in browser)
2. Interactive browser flow (opens browser automatically)

Token cache: ~/.pincer/ms365_token_cache.json
MSAL handles refresh tokens automatically.

Run ``pincer setup-ms365`` to perform the one-time device code auth flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# All scopes required by the full MS365 integration.
ALL_SCOPES: list[str] = [
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Files.ReadWrite.All",
    "Tasks.ReadWrite",
    "Chat.ReadWrite",
    "Contacts.ReadWrite",
    "Notes.ReadWrite.All",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Send",
    "OnlineMeetings.ReadWrite",
]

# Scopes grouped by service.
SERVICE_SCOPES: dict[str, list[str]] = {
    "email": ["Mail.ReadWrite", "Mail.Send"],
    "calendar": ["Calendars.ReadWrite", "OnlineMeetings.ReadWrite"],
    "onedrive": ["Files.ReadWrite.All"],
    "todo": ["Tasks.ReadWrite"],
    "teams": ["Team.ReadBasic.All", "Channel.ReadBasic.All", "ChannelMessage.Send", "Chat.ReadWrite"],
    "contacts": ["Contacts.ReadWrite"],
    "onenote": ["Notes.ReadWrite.All"],
}


def scopes_for_services(services: list[str] | None = None) -> list[str]:
    """Return the deduplicated scope list for the given service names."""
    if services is None:
        return list(ALL_SCOPES)
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
            return result["access_token"]
        return None

    async def get_token(self) -> str:
        """Get valid access token. Uses cache/refresh first, then raises if no cached token."""
        self._ensure_app()

        # Try silent (cached/refresh token)
        token = await asyncio.to_thread(self.get_token_silent)
        if token:
            return token

        raise MS365AuthError("No valid Microsoft 365 token found. Run 'pincer setup-ms365' to authenticate.")

    async def device_code_flow(self) -> dict[str, Any]:
        """Run device code flow: user enters code in browser. Returns MSAL result."""
        self._ensure_app()

        flow = self._app.initiate_device_flow(self._scopes)
        if "user_code" not in flow:
            raise MS365AuthError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")

        self._pending_flow_message = (
            f"To sign in, visit https://microsoft.com/devicelogin and enter code: {flow['user_code']}"
        )

        # This blocks until user completes auth (up to 15 min)
        result = await asyncio.to_thread(self._app.acquire_token_by_device_flow, flow)

        if "access_token" not in result:
            raise MS365AuthError(f"Auth failed: {result.get('error_description', 'unknown error')}")

        self._save_cache()
        return result

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
        return result

    def has_cached_token(self) -> bool:
        """Check if there's any cached account."""
        self._ensure_app()
        return bool(self._app.get_accounts())

    def authenticated_account(self) -> str | None:
        """Return the username from cached accounts, if any."""
        self._ensure_app()
        accounts = self._app.get_accounts()
        if accounts:
            return accounts[0].get("username")
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
