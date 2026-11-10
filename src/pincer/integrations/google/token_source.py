"""
Google OAuth material supplied through environment variables.

``PINCER_GOOGLE_TOKEN_JSON`` holds the authorised-user token JSON (as written
to ``google_workspace_token.json``), either raw or base64-encoded. When set it
takes precedence over the token files in the data volume. Refreshed access
tokens are kept in memory only — Google does not rotate the refresh token, so
nothing needs to be written back.

``PINCER_GOOGLE_CREDENTIALS_JSON`` optionally holds the OAuth client JSON for
the one-time consent flow. The runtime needs an OAuth client only when the
token JSON lacks ``client_id`` / ``client_secret``; it is then taken from
:func:`client_config` (``google_credentials.json`` first, then the env var).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pincer.core.log_once import LogOncePerKey

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_ENV = "PINCER_GOOGLE_TOKEN_JSON"
CREDENTIALS_ENV = "PINCER_GOOGLE_CREDENTIALS_JSON"
#: Token files in the OAuth dir, in lookup order (the second is the legacy calendar-only token).
TOKEN_FILES = ("google_workspace_token.json", "google_token.json")
CREDENTIALS_FILE = "google_credentials.json"

# locate_token runs on every GET /api/integrations and doctor run: each problem is one WARNING, then DEBUG.
_LOG = LogOncePerKey(logger)
_NO_CLIENT = "no 'installed' or 'web' OAuth client with client_id and client_secret"
# client_ids of complete env tokens already checked against the configured OAuth client: the check only
# feeds a once-only warning, so later lookups skip reading google_credentials.json.
_MISMATCH_CHECKED: set[str] = set()


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


def _decode(settings: Any, attr: str) -> tuple[dict[str, Any] | None, str]:
    """``(data, "")`` when the setting decodes, ``(None, reason)`` when it is set but not
    decodable, ``(None, "")`` when it is unset (or not a secret, e.g. a MagicMock)."""
    secret: Any = getattr(settings, attr, None)
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else ""
    if not isinstance(value, str) or not value.strip():
        return None, ""
    try:
        return decode_json_blob(value), ""
    except ValueError as exc:
        return None, str(exc)


def token_env_state(settings: Any) -> str:
    """``"unset"``, ``"invalid"`` (set but not decodable) or ``"ok"`` for ``PINCER_GOOGLE_TOKEN_JSON``."""
    data, error = _decode(settings, "google_token_json")
    if data is not None:
        return "ok"
    return "invalid" if error else "unset"


def env_token_info(settings: Any) -> dict[str, Any] | None:
    """Return the token JSON from ``PINCER_GOOGLE_TOKEN_JSON``, or None if unset/invalid.

    Silent: :func:`locate_token` logs an undecodable value, naming where it falls back to.
    """
    return _decode(settings, "google_token_json")[0]


def env_client_config(settings: Any) -> dict[str, Any] | None:
    """Return the OAuth client JSON from ``PINCER_GOOGLE_CREDENTIALS_JSON``, or None."""
    data, error = _decode(settings, "google_credentials_json")
    if error:
        _LOG.log(f"bad-env-client:{error}", "Ignoring %s: %s", CREDENTIALS_ENV, error)
    return data


def searched_paths(settings: Any, oauth_dir: Path | None = None) -> list[Path]:
    """The token files :func:`locate_token` looks for, in order (for log and doctor messages).

    Pass *oauth_dir* when the caller already resolved ``settings.google_oauth_dir()``.
    """
    oauth_dir = oauth_dir if oauth_dir is not None else settings.google_oauth_dir()
    return [oauth_dir / name for name in TOKEN_FILES]


@dataclass(frozen=True)
class OAuthClient:
    """An OAuth client JSON (``{"installed": {...}}`` or ``{"web": {...}}``) and where it came from."""

    config: dict[str, Any]
    #: ``"file:<path>"`` or ``"env"`` (``PINCER_GOOGLE_CREDENTIALS_JSON``).
    source: str

    @property
    def section(self) -> dict[str, Any]:
        return _client_section(self.config) or {}

    @property
    def client_id(self) -> str:
        return str(self.section.get("client_id", ""))

    @property
    def label(self) -> str:
        return client_source_label(self.source)


def client_source_label(source: str) -> str:
    """``"file:<path>"`` → the path, ``"env"`` → the env var name, ``"token"`` → ``"the token itself"``."""
    if source == "env":
        return CREDENTIALS_ENV
    if source == "token":
        return "the token itself"
    return source.removeprefix("file:")


def _client_section(data: dict[str, Any]) -> dict[str, Any] | None:
    """The ``installed`` / ``web`` client, only with both ``client_id`` and ``client_secret``:
    a client without a secret can neither run the consent flow nor refresh a token."""
    for key in ("installed", "web"):
        section = data.get(key)
        if isinstance(section, dict) and section.get("client_id") and section.get("client_secret"):
            return section
    return None


def oauth_clients(settings: Any, oauth_dir: Path | None = None) -> list[OAuthClient]:
    """Every usable OAuth client, in priority order: ``google_credentials.json`` in
    ``settings.google_oauth_dir()``, then ``PINCER_GOOGLE_CREDENTIALS_JSON``. A source
    without an ``installed``/``web`` client holding ``client_id`` and ``client_secret``
    is logged and skipped. Pass *oauth_dir* when the caller already resolved it.
    """
    clients: list[OAuthClient] = []
    path = (oauth_dir if oauth_dir is not None else settings.google_oauth_dir()) / CREDENTIALS_FILE
    if path.exists():
        try:
            data: Any = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and _client_section(data):
            clients.append(OAuthClient(data, f"file:{path}"))
        else:
            _LOG.log(f"bad-client-file:{path}", "Ignoring %s: %s", path, _NO_CLIENT)
    env = env_client_config(settings)
    if env is not None:
        if _client_section(env):
            clients.append(OAuthClient(env, "env"))
        else:
            _LOG.log("bad-env-client:section", "Ignoring %s: %s", CREDENTIALS_ENV, _NO_CLIENT)
    return clients


def client_config(settings: Any, oauth_dir: Path | None = None) -> OAuthClient | None:
    """The OAuth client: the first of :func:`oauth_clients` (the file wins over the env var), or None.

    The one rule for both the consent flow (``pincer google setup``) and filling in the
    client of an env token that lacks ``client_id``. Pass *oauth_dir* when the caller
    already resolved ``settings.google_oauth_dir()``.
    """
    clients = oauth_clients(settings, oauth_dir)
    return clients[0] if clients else None


def _client_locations(oauth_dir: Path) -> str:
    return f"{oauth_dir / CREDENTIALS_FILE} or {CREDENTIALS_ENV}"


def _env_location(settings: Any, token_info: dict[str, Any]) -> dict[str, Any] | None:
    """``GoogleAuth`` kwargs for an env token, its OAuth client filled in when the token
    lacks one; None when there is none to fill in.

    The client id and secret always come from one client: a token without ``client_id``
    takes both from :func:`client_config` (a stray ``client_secret`` of its own cannot be
    paired with it); a token with ``client_id`` but no secret takes the secret from
    whichever of :func:`oauth_clients` has that id, file or env.
    """
    info = dict(token_info)
    token_client_id = str(info.get("client_id") or "")
    complete = bool(token_client_id and info.get("client_secret"))
    if complete and token_client_id in _MISMATCH_CHECKED:
        return {"token_info": info, "client_source": "token"}
    oauth_dir = settings.google_oauth_dir()
    clients = oauth_clients(settings, oauth_dir)
    primary = clients[0] if clients else None
    if primary is not None and token_client_id and token_client_id != primary.client_id:
        # The token wins: it was issued for its own client, and refreshing it needs that client's secret.
        _LOG.log(
            f"client-mismatch:{token_client_id}:{primary.client_id}",
            "%s was issued for OAuth client %s, but the OAuth client in %s is %s — using the token's own "
            "client; re-running `pincer google setup` would issue a token for a different OAuth client",
            TOKEN_ENV,
            token_client_id,
            primary.label,
            primary.client_id,
        )
    if complete:
        _MISMATCH_CHECKED.add(token_client_id)
        return {"token_info": info, "client_source": "token"}
    if token_client_id:
        client = next((c for c in clients if c.client_id == token_client_id), None)
        keys: tuple[str, ...] = ("client_secret", "token_uri")
    else:
        client = primary
        keys = ("client_id", "client_secret", "token_uri")
    if client is None:
        _LOG.log(
            "env-token-without-client",
            "Google Workspace tools off: %s lacks client_id/client_secret and no matching OAuth client found in %s",
            TOKEN_ENV,
            _client_locations(oauth_dir),
        )
        return None
    for key in keys:
        if client.section.get(key) and (key != "token_uri" or not info.get(key)):
            info[key] = client.section[key]
    return {"token_info": info, "client_source": client.source}


def locate_token(settings: Any) -> dict[str, Any] | None:
    """Return ``GoogleAuth`` keyword arguments for the configured token, or None.

    ``PINCER_GOOGLE_TOKEN_JSON`` wins (no files needed); a token without
    ``client_id`` / ``client_secret`` gets them from :func:`client_config`. An
    undecodable value is logged and the token files are used instead:
    ``google_workspace_token.json`` in ``settings.google_oauth_dir()``, falling
    back to the legacy calendar-only ``google_token.json`` — which still enables
    the calendar part of the Workspace tools. ``google_credentials.json`` is not
    required: the token JSON already carries the OAuth client id and secret.
    """
    token_info, error = _decode(settings, "google_token_json")
    if token_info is not None:
        return _env_location(settings, token_info)

    oauth_dir = settings.google_oauth_dir()
    if error:
        _LOG.log(
            f"bad-env-token:{error}:{oauth_dir}",
            "Ignoring %s: %s; falling back to token files in %s",
            TOKEN_ENV,
            error,
            oauth_dir,
        )
    credentials_path = oauth_dir / CREDENTIALS_FILE
    for token_path in searched_paths(settings, oauth_dir):
        if token_path.exists():
            return {"credentials_path": str(credentials_path), "token_path": str(token_path)}
    return None
