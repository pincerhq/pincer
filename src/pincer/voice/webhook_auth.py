"""
Twilio webhook & WebSocket authentication (Sprint 8, T8.1).

Every inbound voice surface is public internet: the HTTP webhooks
(`/api/apps/twilio/*` plus the deprecated `/voice/*` aliases) and the two
WebSocket endpoints (ConversationRelay `/relay`, Media Streams `/stream/{sid}`).
None of them can carry the dashboard bearer token, so they are authenticated
the way Twilio supports:

HTTP
    `X-Twilio-Signature` — base64(HMAC-SHA1(auth_token, url + sorted form
    params)) for form-encoded bodies, or `url` alone when the URL carries a
    `bodySHA256` query parameter (JSON bodies, e.g. `/relay-webhook`).

WebSocket
    Twilio does not sign the WS upgrade, so the relay/stream URLs we hand
    Twilio in TwiML carry their own short-lived HMAC token (`?t=<unix>&s=<sig>`)
    minted by `signed_ws_query`. An unsigned or stale connect is refused
    *before* `websocket.accept()`.

Replay guard: any request carrying a timestamp (our `t` parameter, or Twilio's
`Timestamp` field) older than `voice_signature_max_age_s` is rejected even if
the signature verifies.

Validation is on by default and can only be turned off explicitly via
`PINCER_VOICE_WEBHOOK_VALIDATE=false` — which `pincer doctor --production`
reports as CRITICAL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

    from pincer.config import Settings

logger = logging.getLogger(__name__)

# Fields Twilio (or we) may use to timestamp a request, in preference order.
_TIMESTAMP_FIELDS = ("t", "Timestamp", "timestamp")


class WebhookAuthError(Exception):
    """Raised when a voice webhook or WS upgrade fails authentication."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── Twilio HTTP signature ────────────────────────────────────────────


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's documented signature: base64(HMAC-SHA1(token, url + k+v sorted)).

    `params` are the POST form fields (empty for GET and for JSON bodies,
    where the URL instead carries `bodySHA256`).
    """
    payload = url
    for key in sorted(params):
        payload += key + params[key]
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def signature_matches(auth_token: str, urls: list[str], params: dict[str, str], signature: str) -> bool:
    """True when `signature` matches any of the candidate URLs.

    Behind a TLS-terminating proxy the URL ASGI reconstructs is not the URL
    Twilio signed (http vs https, internal host vs public hostname), so the
    caller supplies every plausible spelling and any match counts.
    """
    if not signature:
        return False
    return any(hmac.compare_digest(compute_signature(auth_token, url, params), signature) for url in urls)


def body_sha256_matches(url: str, body: bytes) -> bool:
    """For JSON webhooks Twilio appends `bodySHA256=<hex>` to the signed URL."""
    if "bodySHA256=" not in url:
        return True  # nothing to check — form-encoded flow
    query = url.split("?", 1)[1] if "?" in url else ""
    expected = parse_qs(query).get("bodySHA256", [""])[0]
    if not expected:
        return True
    return hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected)


def candidate_urls(request: Request | WebSocket, settings: Settings) -> list[str]:
    """Every spelling of this request's URL that Twilio might have signed."""
    raw = str(request.url)
    urls = [raw]

    # Proxy-corrected: X-Forwarded-Proto/Host survive Caddy/nginx termination.
    headers = request.headers
    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = headers.get("x-forwarded-host", "").split(",")[0].strip() or headers.get("host", "").strip()
    path_qs = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if proto and host:
        urls.append(f"{proto}://{host}{path_qs}")
    if host:
        urls.append(f"https://{host}{path_qs}")

    # Configured public base URL — what the Twilio console actually points at.
    base = str(getattr(settings, "voice_webhook_base_url", "") or "").strip().rstrip("/")
    if base:
        for scheme_swap in (base, base.replace("wss://", "https://").replace("ws://", "http://")):
            urls.append(f"{scheme_swap}{path_qs}")

    # De-duplicate, preserving order.
    return list(dict.fromkeys(urls))


def _replay_age(params: dict[str, str]) -> float | None:
    """Age in seconds of the request's timestamp, or None when it carries none."""
    for field in _TIMESTAMP_FIELDS:
        raw = params.get(field, "")
        if not raw:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return time.time() - ts
    return None


def check_replay(params: dict[str, str], max_age_s: int) -> None:
    """Raise when a timestamped request is older than the replay window.

    Requests with no timestamp pass — Twilio's form webhooks carry none and
    the signature already binds them to a single URL + body.
    """
    age = _replay_age(params)
    if age is None:
        return
    if age > max_age_s:
        raise WebhookAuthError(f"stale request ({age:.0f}s old, max {max_age_s}s)")
    if age < -max_age_s:
        raise WebhookAuthError(f"request timestamp {abs(age):.0f}s in the future")


def _auth_token(settings: Settings | None) -> str:
    if settings is None:
        return ""
    try:
        return str(settings.twilio_auth_token.get_secret_value() or "")
    except AttributeError:  # pragma: no cover — non-SecretStr test doubles
        return str(getattr(settings, "twilio_auth_token", "") or "")


def validation_enabled(settings: Settings | None) -> bool:
    return bool(settings is not None and getattr(settings, "voice_webhook_validate", True))


async def verify_http_request(request: Request, body: bytes, settings: Settings | None) -> None:
    """Authenticate a Twilio HTTP webhook. Raises `WebhookAuthError` on failure.

    Skipped only when validation is explicitly disabled or no Twilio auth token
    is configured (a dev box with no Twilio account cannot receive real
    webhooks anyway); `pincer doctor --production` flags both states CRITICAL.
    """
    if not validation_enabled(settings):
        return
    token = _auth_token(settings)
    if not token:
        logger.warning("Twilio signature validation skipped for %s — no auth token configured", request.url.path)
        return

    params: dict[str, str] = {}
    content_type = request.headers.get("content-type", "")
    if body and "application/x-www-form-urlencoded" in content_type:
        for key, values in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items():
            params[key] = values[0] if values else ""

    urls = candidate_urls(request, settings)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise WebhookAuthError("missing X-Twilio-Signature")
    if not any(body_sha256_matches(url, body) for url in urls):
        raise WebhookAuthError("bodySHA256 mismatch")
    if not signature_matches(token, urls, params, signature):
        raise WebhookAuthError("signature mismatch")

    replay_params = dict(params)
    replay_params.update({k: v for k, v in request.query_params.items() if k not in replay_params})
    check_replay(replay_params, int(getattr(settings, "voice_signature_max_age_s", 300) or 300))


# ── WebSocket upgrade token ──────────────────────────────────────────

# The token is bound to a STABLE surface path, not the concrete request path:
# Media Streams TwiML is built before the call SID exists (Twilio substitutes
# {CallSid} itself) and the deprecated /voice/* aliases reach the same
# handlers, so signing the literal path would never verify.
WS_RELAY_PATH = "/api/apps/twilio/relay"
WS_STREAM_PATH = "/api/apps/twilio/stream"


def _ws_secret(settings: Settings | None) -> str:
    """HMAC key for WS URL tokens — the Twilio auth token, which both the TwiML
    builder and the relay handler already share and which never leaves the host."""
    return _auth_token(settings)


def ws_token(secret: str, path: str, timestamp: int) -> str:
    payload = f"{path}|{timestamp}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def signed_ws_query(settings: Settings | None, path: str, now: int | None = None) -> str:
    """Query string (`?t=…&s=…`) authenticating a WS upgrade for `path`.

    Returns "" when WS auth is off or no secret is available, so TwiML built on
    an unconfigured dev box stays unchanged.
    """
    if settings is not None and not getattr(settings, "voice_ws_auth_required", True):
        return ""
    secret = _ws_secret(settings)
    if not secret:
        return ""
    ts = int(time.time()) if now is None else now
    return "?" + urlencode({"t": str(ts), "s": ws_token(secret, path, ts)})


def verify_ws_upgrade(websocket: WebSocket, settings: Settings | None, path: str | None = None) -> None:
    """Authenticate a WS upgrade before `accept()`. Raises `WebhookAuthError`.

    Accepts either our signed `?t=&s=` token or a valid `X-Twilio-Signature`
    on the upgrade request (Twilio adds one on some integrations).
    """
    if settings is None or not getattr(settings, "voice_ws_auth_required", True):
        return
    secret = _ws_secret(settings)
    if not secret:
        logger.warning("Voice WebSocket auth skipped — no Twilio auth token configured")
        return

    target_path = path or websocket.url.path
    params = dict(websocket.query_params)
    max_age = int(getattr(settings, "voice_signature_max_age_s", 300) or 300)

    signature = websocket.headers.get("X-Twilio-Signature", "")
    if signature and signature_matches(secret, candidate_urls(websocket, settings), {}, signature):
        check_replay(params, max_age)
        return

    supplied = params.get("s", "")
    raw_ts = params.get("t", "")
    if not supplied or not raw_ts:
        raise WebhookAuthError("missing WebSocket auth token")
    try:
        ts = int(raw_ts)
    except ValueError as e:
        raise WebhookAuthError("malformed WebSocket auth timestamp") from e
    if not hmac.compare_digest(ws_token(secret, target_path, ts), supplied):
        raise WebhookAuthError("invalid WebSocket auth token")
    check_replay(params, max_age)


# ── Audit trail ──────────────────────────────────────────────────────


def client_ip(request: Request | WebSocket) -> str:
    """Best-effort client IP, honouring one proxy hop."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


async def audit_rejection(surface: str, ip: str, reason: str, **metadata: Any) -> None:
    """Record a rejected voice request in the audit log (best effort)."""
    logger.warning("Rejected %s from %s: %s", surface, ip, reason)
    try:
        from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger

        audit = await get_audit_logger()
        await audit.log(
            AuditEntry(
                user_id="anonymous",
                action=AuditAction.AUTH_ATTEMPT,
                tool=surface,
                input_summary=reason,
                approved=False,
                ip_address=ip,
                channel="voice",
                metadata={"surface": surface, **metadata},
            )
        )
    except Exception:  # pragma: no cover — auditing must never break a webhook
        logger.debug("Audit logging of webhook rejection failed", exc_info=True)
