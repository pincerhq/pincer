"""
API auth brute-force guard and CORS origin policy (Sprint 8, T8.2).

The dashboard bearer token is a single shared secret on a public HTTPS
endpoint, so an unthrottled `/api/*` is a free offline-speed guessing oracle.
`AuthGuard` gives each client IP a small failure budget and then an
exponentially growing lockout, and every rejected request is audit-logged with
its IP.

The guard is per-IP and in-process — deliberately. Pilot deployments are a
single container behind one TLS proxy; a distributed store would add a
dependency for no gain at this scale. What it must not do is grow without
bound, so expired entries are pruned on every check.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

# Never lock an IP out for longer than this, however many times it fails —
# a locked-out proxy IP would otherwise take the whole dashboard down.
MAX_LOCKOUT_SECONDS = 3600.0

# Public routes: no bearer token required (and therefore never a 401 to count).
PUBLIC_PATHS = ("/api/health", "/api/docs", "/api/openapi.json")

# Sub-apps and webhooks with their own authentication scheme.
SELF_AUTHENTICATED_PREFIXES = (
    "/api/apps/teams/",  # Teams HMAC
    "/api/apps/twilio/",  # X-Twilio-Signature (voice/webhook_auth.py)
)

_LOCALHOST_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8080",
)


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)


class AuthGuard:
    """Per-IP failure budget with exponential lockout."""

    def __init__(self, max_failures: int = 10, lockout_seconds: int = 300) -> None:
        self.max_failures = max(1, max_failures)
        self.lockout_seconds = max(1, lockout_seconds)
        self._ips: dict[str, _Attempts] = {}

    def _prune(self, now: float) -> None:
        # An IP is forgotten once it is unlocked and has been quiet for a full
        # lockout window; keeps the map bounded under scanner traffic.
        stale = [
            ip
            for ip, a in self._ips.items()
            if a.locked_until <= now and now - a.last_seen > self.lockout_seconds + MAX_LOCKOUT_SECONDS
        ]
        for ip in stale:
            del self._ips[ip]

    def retry_after(self, ip: str) -> int:
        """Seconds this IP must wait, or 0 when it may attempt authentication."""
        now = time.monotonic()
        self._prune(now)
        attempts = self._ips.get(ip)
        if attempts is None or attempts.locked_until <= now:
            return 0
        return max(1, int(attempts.locked_until - now) + 1)

    def record_failure(self, ip: str) -> int:
        """Count a failed authentication. Returns the resulting lockout seconds (0 = none)."""
        now = time.monotonic()
        attempts = self._ips.setdefault(ip, _Attempts())
        attempts.failures += 1
        attempts.last_seen = now
        # `max_failures` attempts are free; the lockout starts on the next one.
        over = attempts.failures - self.max_failures - 1
        if over < 0:
            return 0
        backoff = min(self.lockout_seconds * (2**over), MAX_LOCKOUT_SECONDS)
        attempts.locked_until = now + backoff
        logger.warning(
            "API auth lockout: %s failed %d time(s), locked for %.0fs",
            ip,
            attempts.failures,
            backoff,
        )
        return int(backoff)

    def record_success(self, ip: str) -> None:
        """A valid token clears the IP's failure history."""
        self._ips.pop(ip, None)

    def reset(self) -> None:
        self._ips.clear()


def client_ip(request: HTTPConnection) -> str:
    """Client IP honouring a single trusted proxy hop (Caddy/nginx)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


def is_production(settings: Any) -> bool:
    return str(getattr(settings, "environment", "") or "").strip().lower() == "production"


def cors_origins(settings: Any) -> list[str]:
    """Allowed CORS origins.

    Production drops every localhost entry: a browser on a developer machine
    must not be able to talk to the production API with credentials, and a
    stray `http://localhost:*` in an allow-list is a standing CSRF foothold
    for anything running on a victim's machine.
    """
    configured = [
        str(getattr(settings, "dashboard_url", "") or ""),
        str(getattr(settings, "web_chat_url", "") or ""),
        *[o.strip() for o in str(getattr(settings, "cors_extra_origins", "") or "").split(",")],
    ]
    origins = list(configured) if is_production(settings) else [*_LOCALHOST_ORIGINS, *configured]

    return [o for o in dict.fromkeys(origins) if o]


async def audit_auth_failure(ip: str, path: str, reason: str, locked_for: int = 0) -> None:
    """Audit-log a rejected API request (best effort — never breaks the response)."""
    try:
        from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger

        audit = await get_audit_logger()
        await audit.log(
            AuditEntry(
                user_id="anonymous",
                action=AuditAction.AUTH_ATTEMPT,
                tool="api",
                input_summary=f"{reason}: {path}",
                approved=False,
                ip_address=ip,
                channel="api",
                metadata={"path": path, "reason": reason, "locked_for_s": locked_for},
            )
        )
    except Exception:  # pragma: no cover — auditing must not break auth
        logger.debug("Audit logging of auth failure failed", exc_info=True)
