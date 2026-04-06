"""
Authenticated Microsoft Graph HTTP client.

Uses httpx for direct REST calls to Microsoft Graph API.
The msgraph-sdk has complex async patterns that don't integrate cleanly,
so we use the simpler, more reliable approach of direct HTTP calls with
MSAL tokens — same as Microsoft's own recommendations for Python.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pincer.integrations.ms365.auth import MS365Auth

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphAPIError(Exception):
    """Raised when a Graph API call fails."""

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        super().__init__(f"Graph API error {status_code}: {message}")


class GraphClient:
    """
    Authenticated Microsoft Graph REST client.

    Wraps httpx with:
    - Automatic bearer token injection via MSAL
    - Rate limit handling (429 + Retry-After)
    - Error normalization
    - Pagination support
    """

    def __init__(self, auth: MS365Auth) -> None:
        self._auth = auth
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=GRAPH_BASE,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def _get_headers(self) -> dict[str, str]:
        token = await self._auth.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET request to Graph API."""
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST request to Graph API."""
        return await self._request("POST", path, json=json)

    async def patch(
        self,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """PATCH request to Graph API."""
        return await self._request("PATCH", path, json=json)

    async def delete(self, path: str) -> dict[str, Any]:
        """DELETE request to Graph API."""
        return await self._request("DELETE", path)

    async def put(
        self,
        path: str,
        content: bytes | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """PUT request with raw content (used for file uploads)."""
        client = await self._ensure_client()
        headers = await self._get_headers()
        headers["Content-Type"] = content_type

        for attempt in range(3):
            resp = await client.put(path, content=content or b"", headers=headers)
            if resp.status_code == 429 and attempt < 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("Graph API rate limited, retrying in %ds", retry_after)
                await asyncio.sleep(retry_after)
                continue
            break

        if resp.status_code >= 400:
            body = resp.text
            raise GraphAPIError(resp.status_code, body, dict(resp.headers))

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def get_binary(self, path: str) -> bytes:
        """GET request returning raw bytes (for file downloads)."""
        client = await self._ensure_client()
        headers = await self._get_headers()

        for attempt in range(3):
            resp = await client.get(path, headers=headers)
            if resp.status_code == 429 and attempt < 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("Graph API rate limited, retrying in %ds", retry_after)
                await asyncio.sleep(retry_after)
                continue
            break

        if resp.status_code >= 400:
            raise GraphAPIError(resp.status_code, resp.text, dict(resp.headers))
        return resp.content

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request with rate limit retry."""
        client = await self._ensure_client()
        headers = await self._get_headers()

        for attempt in range(3):
            resp = await client.request(method, path, params=params, json=json, headers=headers)
            if resp.status_code == 429 and attempt < 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("Graph API rate limited (attempt %d), retrying in %ds", attempt + 1, retry_after)
                await asyncio.sleep(retry_after)
                continue
            break

        if resp.status_code >= 400:
            body = resp.text
            raise GraphAPIError(resp.status_code, body, dict(resp.headers))

        # 204 No Content (common for DELETE, PATCH, POST actions)
        if resp.status_code == 204 or not resp.content:
            return {}

        return resp.json()

    async def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_items: int = 100,
    ) -> list[dict[str, Any]]:
        """Follow @odata.nextLink until all results are fetched or max_items reached."""
        all_items: list[dict[str, Any]] = []
        result = await self.get(path, params=params)
        all_items.extend(result.get("value", []))

        while len(all_items) < max_items:
            next_link = result.get("@odata.nextLink")
            if not next_link:
                break
            # nextLink is a full URL — use it directly
            client = await self._ensure_client()
            headers = await self._get_headers()
            resp = await client.get(next_link, headers=headers)
            if resp.status_code >= 400:
                break
            result = resp.json()
            all_items.extend(result.get("value", []))

        return all_items[:max_items]

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
