"""
Microsoft 365 OAuth authentication using MSAL.

Supports:
1. Device code flow (headless — user enters code in browser)
2. Interactive browser flow (opens browser automatically)

Each `MS365Auth` instance owns one token cache file, at whatever `cache_path`
its caller gives it — `identity_session.py`'s `IdentitySessionManager` builds
one per identity under `MS365_TOKEN_CACHE_DIR` (default `~/.pincer/ms365_mcp/`).
MSAL handles refresh tokens automatically.

Run ``ms365-mcp-setup`` to perform a one-time device code auth flow for the
`"default"` identity slot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Before the per-identity cache scheme (issue #154), ms365-mcp kept a single
# global token cache here. Confirmed via `git show fca49ff:src/pincer/integrations/ms365/config.py`.
LEGACY_CACHE_PATH = Path.home() / ".pincer" / "ms365_token_cache.json"

# Scopes grouped by service.
SERVICE_SCOPES: dict[str, list[str]] = {
    "email": ["Mail.ReadWrite", "Mail.Send"],
    "calendar": ["Calendars.ReadWrite"],
    "onedrive": ["Files.ReadWrite.All"],
    "todo": ["Tasks.ReadWrite"],
    "contacts": ["Contacts.ReadWrite"],
    "onenote": ["Notes.ReadWrite.All"],
    "directory": ["User.ReadBasic.All"],
}


def scopes_for_services(services: list[str] | None = None) -> list[str]:
    """Return the deduplicated scope list for the given service names."""
    if services is None:
        return ["User.Read"] + [scope for scopes in SERVICE_SCOPES.values() for scope in scopes]
    seen: set[str] = set()
    result: list[str] = ["User.Read"]
    seen.add("User.Read")
    for svc in services:
        for scope in SERVICE_SCOPES.get(svc, []):
            if scope not in seen:
                seen.add(scope)
                result.append(scope)
    return result


def _is_plaintext_cache(raw: bytes) -> bool:
    """Return True if `raw` looks like a real MSAL cache (JSON object).

    `msal.token_cache.SerializableTokenCache.serialize()` always emits
    `json.dumps(self._cache, indent=4)` where `self._cache` starts as `{}` —
    i.e. the real on-disk format is always a JSON object, never ciphertext
    (which won't parse as JSON at all) or a bare non-dict value.
    """
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict)


def migrate_plaintext_caches(cache_dir: Path, fernet: Fernet) -> list[str]:
    """Encrypt any already-on-disk per-identity caches still stored as plaintext.

    Per-identity caches are only touched lazily, inside `MS365Auth._load_cache`,
    when that identity's *next* tool call happens to run — turning on
    MS365_TOKEN_ENCRYPTION_KEY does nothing for identities that aren't used
    again, leaving their tokens sitting in plaintext on disk indefinitely. Call
    this once at server startup (when a Fernet key is configured) to sweep
    `cache_dir` and bring every existing cache under encryption immediately,
    regardless of whether its identity ever reconnects.

    Returns the list of cache file names that were migrated.
    """
    migrated: list[str] = []
    if not cache_dir.is_dir():
        return migrated

    for path in sorted(cache_dir.glob("*_token_cache.json")):
        raw = path.read_bytes()
        try:
            fernet.decrypt(raw)
        except InvalidToken:
            pass
        else:
            continue  # already encrypted

        if not _is_plaintext_cache(raw):
            logger.warning(
                "Token cache %s is neither valid ciphertext nor plaintext JSON "
                "(wrong/rotated key, or corrupt file) — leaving it untouched.",
                path,
            )
            continue

        path.write_bytes(fernet.encrypt(raw))
        os.chmod(path, 0o600)
        migrated.append(path.name)
        logger.info("Encrypted existing plaintext token cache: %s", path)

    return migrated


def _import_legacy_cache(target_cache_path: Path, fernet: Fernet | None) -> None:
    """Import the pre-per-identity single-account cache into `target_cache_path`, once.

    No-ops if the legacy file doesn't exist, or `target_cache_path` already
    exists. The old file is deleted after a successful import — not kept as
    a backup.
    """
    if not LEGACY_CACHE_PATH.exists() or target_cache_path.exists():
        return
    raw = LEGACY_CACHE_PATH.read_bytes()
    data = fernet.encrypt(raw) if fernet is not None else raw
    target_cache_path.parent.mkdir(parents=True, exist_ok=True)
    target_cache_path.write_bytes(data)
    target_cache_path.chmod(0o600)
    LEGACY_CACHE_PATH.unlink()
    logger.info(
        "Migrated legacy Microsoft 365 token cache %s to %s (old file removed)",
        LEGACY_CACHE_PATH,
        target_cache_path,
    )


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
        cache_path: str,
        tenant_id: str = "common",
        services: list[str] | None = None,
        fernet: Fernet | None = None,
        import_legacy_cache: bool = False,
    ) -> None:
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._cache_path = Path(os.path.expanduser(cache_path))
        self._scopes = scopes_for_services(services)
        self._app: Any = None  # msal.PublicClientApplication
        self._cache: Any = None  # msal.SerializableTokenCache
        self._pending_flow_message: str = ""
        self._fernet = fernet
        self._import_legacy_cache = import_legacy_cache

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
        if not self._cache_path.exists():
            if self._import_legacy_cache:
                _import_legacy_cache(self._cache_path, self._fernet)
            if not self._cache_path.exists():
                return
        raw = self._cache_path.read_bytes()
        if self._fernet is not None:
            try:
                raw = self._fernet.decrypt(raw)
            except InvalidToken:
                if not _is_plaintext_cache(raw):
                    logger.warning(
                        "Token cache at %s could not be decrypted (wrong/rotated key or "
                        "corrupt file) — treating as no cached token; re-authentication "
                        "will be required.",
                        self._cache_path,
                    )
                    return
                logger.info(
                    "Migrating legacy unencrypted token cache to encrypted storage: %s",
                    self._cache_path,
                )
                self._cache.deserialize(raw.decode("utf-8"))
                self._write_cache_bytes(self._fernet.encrypt(raw))
                return
        try:
            self._cache.deserialize(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                "Token cache at %s is not readable as plaintext JSON (likely still "
                "encrypted from a previous run with MS365_TOKEN_ENCRYPTION_KEY set, "
                "which is now unset) — deleting it; re-authentication will be required.",
                self._cache_path,
            )
            self._cache_path.unlink(missing_ok=True)

    def _write_cache_bytes(self, data: bytes) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(data)
        os.chmod(self._cache_path, 0o600)

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        data = self._cache.serialize().encode("utf-8")
        if self._fernet is not None:
            data = self._fernet.encrypt(data)
        self._write_cache_bytes(data)

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

    def initiate_device_flow_sync(self) -> dict[str, Any]:
        """Start the device code flow and return immediately (non-blocking).

        Does **not** wait for the user to sign in — the returned dict (MSAL's
        flow, with a normalized ``expires_at``) must be passed to
        ``complete_device_flow`` / ``complete_device_flow_sync`` to actually
        poll Microsoft for completion. Use this (instead of
        ``device_code_flow_sync``) when the caller needs to show the
        verification URL/code right away without blocking on the whole flow —
        e.g. a per-identity tool call, where the block would tie up the request.
        """
        import time

        self._ensure_app()

        flow = self._app.initiate_device_flow(self._scopes)
        if "user_code" not in flow:
            raise MS365AuthError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")

        msg = flow.get(
            "message",
            f"To sign in, visit https://microsoft.com/devicelogin and enter code: {flow['user_code']}",
        )
        self._pending_flow_message = msg

        expires_in = flow.get("expires_in", 900)
        expires_at = flow.get("expires_at", time.time() + expires_in)
        flow["expires_at"] = expires_at  # normalize: callers can always rely on this key
        logger.debug(
            "Device flow: expires_in=%s expires_at=%.0f now=%.0f remaining=%.0fs",
            expires_in,
            expires_at,
            time.time(),
            expires_at - time.time(),
        )
        return dict(flow)  # type: ignore[no-any-return]

    async def initiate_device_flow(self) -> dict[str, Any]:
        """Async wrapper around initiate_device_flow_sync."""
        return await asyncio.to_thread(self.initiate_device_flow_sync)

    def complete_device_flow_sync(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Block until the device code flow started by ``initiate_device_flow`` completes.

        Polls Microsoft until the user signs in, the flow is declined, or it
        expires (MSAL enforces ``flow["expires_at"]`` internally) — this is the
        long-running half split out of the old ``device_code_flow_sync``.
        """
        self._ensure_app()

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

    async def complete_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Async wrapper around complete_device_flow_sync."""
        return await asyncio.to_thread(self.complete_device_flow_sync, flow)

    def device_code_flow_sync(self) -> dict[str, Any]:
        """Run the full device code flow synchronously: start + block until completion.

        Call this BEFORE starting an asyncio event loop so that all MSAL calls
        execute in the same thread with no asyncio/threading indirection.
        stdout is reserved for the MCP stdio protocol; the user-code message is
        printed to stderr. Used by the CLI setup wizard (``ms365-mcp-setup``);
        the lazy per-identity flow (``identity_session.py``) uses
        ``initiate_device_flow``/``complete_device_flow`` directly instead so it
        never blocks a tool call.
        """
        flow = self.initiate_device_flow_sync()
        print(f"\n{self._pending_flow_message}\n", file=sys.stderr, flush=True)
        return self.complete_device_flow_sync(flow)

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
