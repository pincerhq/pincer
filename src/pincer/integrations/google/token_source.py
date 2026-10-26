"""
Google OAuth material supplied through environment variables.

``PINCER_GOOGLE_TOKEN_JSON`` holds the authorised-user token JSON (as written
to ``google_workspace_token.json``), either raw or base64-encoded. When set it
takes precedence over the token files in the data volume. Refreshed access
tokens are kept in memory only — Google does not rotate the refresh token, so
nothing needs to be written back.

``PINCER_GOOGLE_CREDENTIALS_JSON`` optionally holds the OAuth client JSON for
the one-time consent flow; the runtime does not need it because the token JSON
already carries ``client_id`` / ``client_secret``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_ENV = "PINCER_GOOGLE_TOKEN_JSON"
CREDENTIALS_ENV = "PINCER_GOOGLE_CREDENTIALS_JSON"


def decode_json_blob(value: str) -> dict[str, Any]:
    """Decode a JSON object given raw or base64 (standard or urlsafe).

    Raises ``ValueError`` without echoing *value* (it is a secret).
    """
    value = value.strip()
    padded = value + "=" * (-len(value) % 4)
    candidates = (
        lambda: value,
        lambda: base64.b64decode(padded, validate=True),
        lambda: base64.urlsafe_b64decode(padded),
    )
    for candidate in candidates:
        try:
            data = json.loads(candidate())
        except (binascii.Error, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("expected a JSON object, raw or base64-encoded")


def encode_json_blob(data: dict[str, Any] | str) -> str:
    """Encode token JSON as single-line base64, safe for .env files and secret stores."""
    raw = data if isinstance(data, str) else json.dumps(data)
    return base64.b64encode(raw.encode()).decode()


def _read(settings: Any, attr: str, env_name: str) -> dict[str, Any] | None:
    secret: Any = getattr(settings, attr, None)
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else ""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return decode_json_blob(value)
    except ValueError as exc:
        logger.warning("Ignoring %s: %s", env_name, exc)
        return None


def env_token_info(settings: Any) -> dict[str, Any] | None:
    """Return the token JSON from ``PINCER_GOOGLE_TOKEN_JSON``, or None if unset/invalid."""
    return _read(settings, "google_token_json", TOKEN_ENV)


def env_client_config(settings: Any) -> dict[str, Any] | None:
    """Return the OAuth client JSON from ``PINCER_GOOGLE_CREDENTIALS_JSON``, or None."""
    return _read(settings, "google_credentials_json", CREDENTIALS_ENV)


def locate_token(settings: Any) -> dict[str, Any] | None:
    """Return ``GoogleAuth`` keyword arguments for the configured token, or None.

    ``PINCER_GOOGLE_TOKEN_JSON`` wins (no files needed). Otherwise uses
    ``google_workspace_token.json`` in ``settings.google_oauth_dir()``, falling
    back to the legacy calendar-only ``google_token.json`` — which still enables
    the calendar part of the Workspace tools. ``google_credentials.json`` is not
    required: the token JSON already carries the OAuth client id and secret.
    """
    token_info = env_token_info(settings)
    if token_info is not None:
        return {"token_info": token_info}

    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    for name in ("google_workspace_token.json", "google_token.json"):
        token_path = oauth_dir / name
        if token_path.exists():
            return {"credentials_path": str(credentials_path), "token_path": str(token_path)}
    return None
