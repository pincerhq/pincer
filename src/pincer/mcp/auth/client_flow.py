"""Pincer as an OAuth 2.1 client — connects to OAuth-protected MCP servers."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import urllib.parse
import webbrowser
from typing import TYPE_CHECKING, Any

from pincer.mcp.auth.pkce import generate_code_challenge, generate_code_verifier

if TYPE_CHECKING:
    from pincer.mcp.auth.token_store import TokenStore

logger = logging.getLogger("pincer.mcp.auth.client_flow")

_CALLBACK_PORT = 18801
_CALLBACK_PATH = "/callback"
_CALLBACK_TIMEOUT = 300  # seconds


class _CallbackServer:
    """Minimal asyncio HTTP server to capture the OAuth callback."""

    def __init__(self) -> None:
        self._params: dict[str, str] | None = None
        self._event = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None

    async def start(self, port: int = _CALLBACK_PORT) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = line.decode("ascii", errors="replace").split()
            if len(parts) >= 2:
                path = parts[1]
                parsed = urllib.parse.urlparse(path)
                self._params = dict(urllib.parse.parse_qsl(parsed.query))
            else:
                self._params = {}
        except Exception:
            self._params = {}
        finally:
            body = b"<h1>Authorization complete. You may close this window.</h1>"
            headers = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Connection: close\r\n"
                + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                + b"\r\n"
            )
            try:
                writer.write(headers + body)
                await writer.drain()
            except Exception:
                pass
            writer.close()
        self._event.set()

    async def wait(self, timeout: float = _CALLBACK_TIMEOUT) -> dict[str, str]:
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        return self._params or {}


class MCPOAuthClient:
    """
    Handles the OAuth 2.1 client side for connecting to an OAuth-protected MCP server.

    Supports:
    - PKCE authorization_code flow (for interactive clients)
    - client_credentials flow (for server-to-server)
    - Automatic token refresh
    """

    def __init__(
        self,
        mcp_server_url: str,
        token_store: TokenStore,
        client_config: dict[str, Any] | None = None,
    ) -> None:
        self._server_url = mcp_server_url.rstrip("/")
        self._token_store = token_store
        self._client_config = client_config or {}
        self._as_metadata: dict[str, Any] | None = None
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def get_headers(self) -> dict[str, str]:
        """Return Authorization headers, performing OAuth flow if needed."""
        resource = self._server_url

        # Try stored access token
        access_token = self._token_store.get_access_token(resource)
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}

        # Try refresh
        refresh_token = self._token_store.get_refresh_token(resource)
        if refresh_token:
            try:
                new_access = await self._refresh(refresh_token)
                return {"Authorization": f"Bearer {new_access}"}
            except Exception as e:
                logger.debug("Refresh failed: %s", e)
                self._token_store.clear(resource)

        # Discover metadata
        await self._discover_metadata()

        # Choose flow
        if self._client_config.get("client_secret") or self._client_config.get("grant_type") == "client_credentials":
            token = await self._client_credentials_flow()
        else:
            token = await self._full_pkce_flow()

        return {"Authorization": f"Bearer {token}"}

    async def _discover_metadata(self) -> None:
        """Discover OAuth server metadata via RFC 9728 + RFC 8414."""
        import httpx

        parsed = urllib.parse.urlparse(self._server_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # RFC 9728: Protected Resource Metadata
                r = await client.get(f"{self._server_url}/.well-known/oauth-protected-resource")
                if r.status_code == 200:
                    prm = r.json()
                    auth_servers = prm.get("authorization_servers", [])
                    as_url = auth_servers[0] if auth_servers else base
                else:
                    as_url = base

                # RFC 8414: Authorization Server Metadata
                r2 = await client.get(f"{as_url}/.well-known/oauth-authorization-server")
                if r2.status_code == 200:
                    self._as_metadata = r2.json()
                else:
                    # Fallback: construct defaults per MCP spec
                    self._as_metadata = {
                        "issuer": as_url,
                        "authorization_endpoint": f"{as_url}/authorize",
                        "token_endpoint": f"{as_url}/token",
                        "registration_endpoint": f"{as_url}/register",
                    }
        except Exception as e:
            logger.warning("OAuth metadata discovery failed: %s", e)
            self._as_metadata = {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
            }

    async def _register_client(self) -> None:
        """Dynamically register this client (RFC 7591)."""
        import httpx

        reg_endpoint = self._as_metadata.get("registration_endpoint") if self._as_metadata else None
        if not reg_endpoint:
            return

        payload = {
            "client_name": "Pincer Agent",
            "redirect_uris": [f"http://127.0.0.1:{_CALLBACK_PORT}{_CALLBACK_PATH}"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(reg_endpoint, json=payload)
                if r.status_code in (200, 201):
                    data = r.json()
                    self._client_id = data.get("client_id")
                    self._client_secret = data.get("client_secret")
                    logger.info("Registered OAuth client: %s", self._client_id)
        except Exception as e:
            logger.warning("Client registration failed: %s", e)

    async def _full_pkce_flow(self) -> str:
        """
        Full PKCE authorization_code flow:
        1. Register client (DCR)
        2. Build authorize URL
        3. Start callback server
        4. Open browser
        5. Wait for callback
        6. Exchange code for token
        """
        import httpx

        if not self._client_id:
            await self._register_client()

        client_id = self._client_id or "pincer-agent"

        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        state = secrets.token_hex(16)
        redirect_uri = f"http://127.0.0.1:{_CALLBACK_PORT}{_CALLBACK_PATH}"

        as_meta = self._as_metadata or {}
        authorize_url = as_meta.get("authorization_endpoint", "")
        token_endpoint = as_meta.get("token_endpoint", "")

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": self._server_url,
            "scope": "tools:all offline_access",
        }
        full_url = f"{authorize_url}?{urllib.parse.urlencode(params)}"

        # Start callback server
        cb_server = _CallbackServer()
        await cb_server.start(_CALLBACK_PORT)

        try:
            logger.info("Opening browser for OAuth authorization: %s", full_url)
            webbrowser.open(full_url)

            # Wait for callback
            cb_params = await cb_server.wait(_CALLBACK_TIMEOUT)
        finally:
            await cb_server.stop()

        if "error" in cb_params:
            raise RuntimeError(f"OAuth error: {cb_params.get('error')}: {cb_params.get('error_description', '')}")

        returned_state = cb_params.get("state", "")
        if not secrets.compare_digest(returned_state, state):
            raise RuntimeError("OAuth state mismatch — possible CSRF attack")

        code = cb_params.get("code", "")
        if not code:
            raise RuntimeError("No authorization code in callback")

        # Exchange code for token
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            r = await http_client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": self._server_url,
                },
            )
            r.raise_for_status()
            token_data = r.json()

        access_token: str = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        expires_in = int(token_data.get("expires_in", 3600))

        self._token_store.store(
            resource=self._server_url,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=int(time.time()) + expires_in,
        )
        return access_token

    async def _refresh(self, refresh_token: str) -> str:
        """Refresh an access token using a refresh token."""
        import httpx

        if not self._as_metadata:
            await self._discover_metadata()

        as_meta = self._as_metadata or {}
        token_endpoint = as_meta.get("token_endpoint", "")
        client_id = self._client_id or "pincer-agent"

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            r.raise_for_status()
            data = r.json()

        access_token: str = data["access_token"]
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in = int(data.get("expires_in", 3600))

        self._token_store.store(
            resource=self._server_url,
            access_token=access_token,
            refresh_token=new_refresh,
            expires_at=int(time.time()) + expires_in,
        )
        return access_token

    async def _client_credentials_flow(self) -> str:
        """client_credentials flow for confidential clients / server-to-server."""
        import httpx

        if not self._as_metadata:
            await self._discover_metadata()

        as_meta = self._as_metadata or {}
        token_endpoint = as_meta.get("token_endpoint", "")
        client_id = self._client_config.get("client_id", "")
        client_secret = self._client_config.get("client_secret", "")

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "resource": self._server_url,
                    "scope": self._client_config.get("scope", "tools:read"),
                },
            )
            r.raise_for_status()
            data = r.json()

        access_token: str = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))

        self._token_store.store(
            resource=self._server_url,
            access_token=access_token,
            refresh_token=None,
            expires_at=int(time.time()) + expires_in,
        )
        return access_token
