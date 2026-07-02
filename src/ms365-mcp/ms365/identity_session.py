"""
Per-identity session registry.

Each distinct ``identity`` — an opaque string carried in the MCP ``_meta``
envelope of a tool call (see ``ms365_server.py``'s ``_identity_from_ctx``) —
gets its own ``MS365Auth``/``GraphClient`` pair, created lazily on first use
rather than at process boot. This is what lets one ms365-mcp server process
serve multiple Microsoft accounts instead of the single boot-time account it
used to be limited to.

``identity`` is untrusted, opaque, and host-agnostic: never assume it *is* a
Pincer ``pincer_user_id`` or has any particular shape. A missing identity
(e.g. a bare MCP client that doesn't send one) falls back to a fixed
``DEFAULT_IDENTITY`` slot, so the server still works standalone/single-user.

Authentication is non-blocking: when an identity has no cached token,
``get_or_create`` does **not** wait for the user to sign in. It starts the
device-code flow, spawns a background task to poll Microsoft for completion,
and immediately raises ``AuthPendingError`` carrying the verification
URL/code. Tool registration (``ms365_server.py``) doesn't catch this — it
propagates out of the tool call as FastMCP's normal tool-failure signal
(`isError=True`), so the caller sees the sign-in instructions as the tool's
result without the request ever blocking. A second call while a flow is
already in flight gets the same instructions again rather than starting a
competing device code.

Once the background task finishes, it best-effort pushes an MCP notification
(``ctx.info``/``ctx.error``) back over the connection that was open when the
flow started — this is reliably delivered over stdio (confirmed: a `Context`
captured during a tool call remains valid and deliverable from a detached
background task on the same connection), but streamable-HTTP clients don't
all keep a channel open to receive unsolicited server pushes, so it may not
reach every HTTP caller. ``status_for()`` / the ``ms365__check_auth_status``
tool (`ms365_server.py`) is the transport-agnostic fallback: any caller can
poll it after seeing the pending message, regardless of whether the push
notification reached them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp import Context

    from ms365.auth import MS365Auth
    from ms365.graph_client import GraphClient

logger = logging.getLogger(__name__)

DEFAULT_IDENTITY = "default"

# Logger name notifications are tagged with, so a listening MCP client can
# distinguish "this identity's auth just resolved" from ordinary log noise.
NOTIFICATION_LOGGER_NAME = "ms365-mcp"

# Keep identity slugs safe as a filename component: alphanumerics, and a small
# set of separators that are common in emails/usernames (`_`, `.`, `@`, `-`).
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.@-]")


class AuthPendingError(Exception):
    """Raised when an identity has no usable token yet.

    ``str(error)`` is a user-facing message (device-code sign-in instructions,
    or an "already in progress" notice) meant to be shown to the caller as-is —
    e.g. by letting it propagate out of a tool call as the tool's result text.
    """


def sanitize_identity(identity: str | None) -> str:
    """Return a filesystem-safe, non-empty identity slug.

    ``identity`` is untrusted input from whatever MCP host set it — never
    assume it's already safe to use as a path component. Falls back to
    ``DEFAULT_IDENTITY`` for a missing/empty/all-unsafe value.
    """
    if not identity:
        return DEFAULT_IDENTITY
    cleaned = _UNSAFE_CHARS.sub("_", identity).strip("._")
    return cleaned or DEFAULT_IDENTITY


@dataclass
class _PendingFlow:
    task: asyncio.Task[None]
    message: str
    expires_at: float


@dataclass
class AuthStatus:
    """Result of ``IdentitySessionManager.status_for()`` — see ``ms365__check_auth_status``."""

    state: Literal["signed_in", "pending", "not_signed_in"]
    message: str


class IdentitySessionManager:
    """Lazily creates and caches one authenticated GraphClient per identity."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        cache_dir: Path,
        services: list[str] | None = None,
    ) -> None:
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._cache_dir = cache_dir
        self._services = services
        self._auths: dict[str, MS365Auth] = {}
        self._clients: dict[str, GraphClient] = {}
        self._pending: dict[str, _PendingFlow] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def cache_path_for(self, identity: str) -> Path:
        """Return the per-identity token cache path (sanitizes ``identity``)."""
        slug = sanitize_identity(identity)
        return self._cache_dir / f"{slug}_token_cache.json"

    async def _lock_for(self, slug: str) -> asyncio.Lock:
        """Get (creating if needed) the per-identity lock, guarded against races."""
        async with self._locks_guard:
            lock = self._locks.get(slug)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[slug] = lock
            return lock

    def _auth_for(self, slug: str) -> MS365Auth:
        auth = self._auths.get(slug)
        if auth is None:
            from ms365.auth import MS365Auth

            auth = MS365Auth(
                client_id=self._client_id,
                tenant_id=self._tenant_id,
                cache_path=str(self.cache_path_for(slug)),
                services=self._services,
            )
            self._auths[slug] = auth
        return auth

    async def get_or_create(self, identity: str | None, ctx: Context | None = None) -> GraphClient:
        """Return the identity's GraphClient, or raise ``AuthPendingError``.

        Concurrent calls for the same (new) identity are serialized by a
        per-identity lock so only one device-code flow is ever started for it.
        ``ctx`` (if given) is kept for the duration of a newly-started flow so
        its background task can push a best-effort completion notification —
        see the module docstring for why this isn't the only delivery path.
        """
        slug = sanitize_identity(identity)

        existing = self._clients.get(slug)
        if existing is not None:
            return existing

        lock = await self._lock_for(slug)
        async with lock:
            existing = self._clients.get(slug)
            if existing is not None:
                return existing

            auth = self._auth_for(slug)

            pending = self._pending.get(slug)
            if pending is not None:
                if time.time() < pending.expires_at:
                    raise AuthPendingError(f"Sign-in already in progress for this account. {pending.message}")
                # Flow expired without anyone consuming its result — drop it and retry fresh.
                logger.info("Identity=%r: previous device code flow expired — starting a new one", slug)
                del self._pending[slug]

            if auth.has_cached_token():
                from ms365.graph_client import GraphClient

                client = GraphClient(auth)
                self._clients[slug] = client
                return client

            logger.info("No cached token for identity=%r — starting device code flow...", slug)
            flow = await auth.initiate_device_flow()
            message = str(flow["message"])
            expires_at = float(flow["expires_at"])
            task = asyncio.create_task(self._complete_flow(slug, auth, flow, ctx))
            self._pending[slug] = _PendingFlow(task=task, message=message, expires_at=expires_at)
            raise AuthPendingError(
                f"{message} Once signed in, retry your request (or call ms365__check_auth_status)."
            )

    async def _complete_flow(self, slug: str, auth: MS365Auth, flow: dict[str, Any], ctx: Context | None) -> None:
        """Background task: poll Microsoft until the device code flow resolves."""
        from ms365.auth import MS365AuthError
        from ms365.graph_client import GraphClient

        try:
            await auth.complete_device_flow(flow)
        except MS365AuthError as exc:
            logger.exception("Identity=%r: device code sign-in failed", slug)
            await self._notify(ctx, slug, f"Microsoft 365 sign-in failed: {exc}", is_error=True)
            return
        else:
            self._clients[slug] = GraphClient(auth)
            logger.info("Identity=%r: device code sign-in completed", slug)
            await self._notify(ctx, slug, "Microsoft 365 sign-in complete — you can retry your request now.")
        finally:
            self._pending.pop(slug, None)

    async def _notify(self, ctx: Context | None, slug: str, message: str, is_error: bool = False) -> None:
        """Best-effort push notification; see module docstring for delivery caveats.

        ``extra={"identity": slug}`` lets a listening MCP client work out which
        caller this pertains to — a single stdio connection typically
        multiplexes many identities, so the notification can't rely on
        connection identity alone.
        """
        if ctx is None:
            return
        try:
            if is_error:
                await ctx.error(message, logger_name=NOTIFICATION_LOGGER_NAME, extra={"identity": slug})
            else:
                await ctx.info(message, logger_name=NOTIFICATION_LOGGER_NAME, extra={"identity": slug})
        except Exception:
            logger.debug("Failed to push auth-completion notification", exc_info=True)

    def status_for(self, identity: str | None) -> AuthStatus:
        """Transport-agnostic status check — see ``ms365__check_auth_status``."""
        slug = sanitize_identity(identity)

        if slug in self._clients:
            return AuthStatus("signed_in", "Signed in to Microsoft 365.")

        pending = self._pending.get(slug)
        if pending is not None and time.time() < pending.expires_at:
            return AuthStatus("pending", f"Sign-in in progress. {pending.message}")

        return AuthStatus(
            "not_signed_in",
            "Not signed in to Microsoft 365 yet. Call any Microsoft 365 tool to start sign-in.",
        )

    def known_identities(self) -> list[str]:
        """Return the identity slugs with an already-created client (for diagnostics/tests)."""
        return list(self._clients)

    def pending_identities(self) -> list[str]:
        """Return the identity slugs with an in-flight device-code flow (for diagnostics/tests)."""
        return list(self._pending)
