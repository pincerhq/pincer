"""
Google API service object factory with per-service caching.

Each Google API needs its own service object built via
``googleapiclient.discovery.build``.  This factory caches them for 30 minutes
so we don't rebuild on every tool call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.google.auth import GoogleAuth

logger = logging.getLogger(__name__)

# (api_name, version) tuples for each logical service.
_API_VERSIONS: dict[str, tuple[str, str]] = {
    "gmail": ("gmail", "v1"),
    "calendar": ("calendar", "v3"),
    "drive": ("drive", "v3"),
    "docs": ("docs", "v1"),
    "sheets": ("sheets", "v4"),
    "slides": ("slides", "v1"),
    "tasks": ("tasks", "v1"),
    "contacts": ("people", "v1"),
    "meet": ("meet", "v2"),
}

_CACHE_TTL = 1800  # 30 minutes


class GoogleServiceFactory:
    """Build and cache Google API service objects.

    Usage::

        factory = GoogleServiceFactory(auth)
        gmail_svc = await factory.get("gmail")
        result = await asyncio.to_thread(
            gmail_svc.users().messages().list(userId="me").execute
        )
    """

    def __init__(self, auth: "GoogleAuth") -> None:
        self._auth = auth
        # service_name → (service_object, created_at_timestamp)
        self._cache: dict[str, tuple[Any, float]] = {}

    async def get(self, service_name: str) -> Any:
        """Return a cached (or freshly built) Google API service object."""
        entry = self._cache.get(service_name)
        if entry is not None:
            svc, created_at = entry
            if time.monotonic() - created_at < _CACHE_TTL:
                return svc

        if service_name not in _API_VERSIONS:
            raise ValueError(f"Unknown Google service: {service_name!r}. Choose from: {list(_API_VERSIONS)}")

        # get_credentials is sync (may do I/O for refresh) — run in thread
        creds = await asyncio.to_thread(self._auth.get_credentials)
        api, version = _API_VERSIONS[service_name]

        from googleapiclient.discovery import build

        svc = await asyncio.to_thread(
            build, api, version, credentials=creds, cache_discovery=False
        )
        self._cache[service_name] = (svc, time.monotonic())
        logger.debug("Built Google API service: %s %s", api, version)
        return svc

    def invalidate(self, service_name: str | None = None) -> None:
        """Evict one or all cached services (e.g. after a token refresh)."""
        if service_name is None:
            self._cache.clear()
        else:
            self._cache.pop(service_name, None)

    @property
    def auth(self) -> "GoogleAuth":
        return self._auth
