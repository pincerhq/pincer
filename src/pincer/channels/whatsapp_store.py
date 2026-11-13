"""The WhatsApp (whatsmeow) session store: URL handling and device lookup.

whatsmeow keeps its session either in a SQLite file or in Postgres, behind a
lib/pq (Go) URL. Pincer only reads it, to find the stored device for
PINCER_WHATSAPP_JID before neonize opens it. All raw `sqlite3` and `psycopg`
access for that lives here: the store belongs to whatsmeow, not to Pincer's
database, so the repository layer does not apply.

The URL helpers are not `db/engine.py`'s: those translate a SQLAlchemy URL for
asyncpg, these translate a lib/pq URL for libpq.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from pincer.config.channels import is_postgres_url
from pincer.db.engine import escape_option_value

try:
    from neonize.utils import build_jid
except ImportError:  # pragma: no cover - neonize is an optional extra
    build_jid = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# The neonize session key for a Postgres store. Without an explicit uuid
# neonize falls back to `name`, which for Postgres is the URL with its password.
WA_SESSION_UUID = "pincer-wa"
# Seconds to wait for Postgres when looking up the stored device.
STORE_LOOKUP_CONNECT_TIMEOUT = 10

# lib/pq's own client-side parameters: they mean nothing to the server either.
_LIB_PQ_CLIENT_ONLY = frozenset({"binary_parameters", "disable_prepared_binary_result"})
# Used only when psycopg (and so libpq's own list) is unavailable.
_LIBPQ_KEYWORDS_FALLBACK = frozenset(
    {
        "application_name", "channel_binding", "client_encoding", "connect_timeout", "dbname",
        "fallback_application_name", "gssencmode", "gsslib", "host", "hostaddr", "keepalives",
        "keepalives_count", "keepalives_idle", "keepalives_interval", "krbsrvname", "load_balance_hosts",
        "max_protocol_version", "min_protocol_version", "options", "passfile", "password", "port",
        "replication", "require_auth", "requirepeer", "service", "ssl_max_protocol_version",
        "ssl_min_protocol_version", "sslcert", "sslcertmode", "sslcompression", "sslcrl", "sslcrldir",
        "sslkey", "sslmode", "sslnegotiation", "sslpassword", "sslrootcert", "sslsni",
        "target_session_attrs", "tcp_user_timeout", "user",
    }
)  # fmt: skip
_libpq_keywords_cache: frozenset[str] | None = None

# A password shorter than this is replaced only as a whole token (not inside
# a longer run of word characters), so `54` never garbles the `5432` port.
_MIN_LITERAL_SECRET_LEN = 6
_URL_USERINFO_RE = re.compile(r"(://[^:/@\s]*:)[^@\s]*@")
_PASSWORD_PARAM_RE = re.compile(r"(?i)\bpassword=[^&\s]*")
_STORE_PLACEHOLDER = "<session db url>"


def _libpq_keywords() -> frozenset[str]:
    """The connection keywords this libpq accepts."""
    global _libpq_keywords_cache
    if _libpq_keywords_cache is None:
        try:
            from psycopg import pq

            keywords = frozenset(d.keyword.decode() for d in pq.Conninfo.get_defaults())
        except (ImportError, AttributeError):
            return _LIBPQ_KEYWORDS_FALLBACK
        _libpq_keywords_cache = keywords
    return _libpq_keywords_cache


def libpq_conninfo(url: str) -> tuple[str, str | None]:
    """Make a lib/pq session URL usable by libpq; return (url, search_path).

    lib/pq sends every URL parameter it doesn't know (`search_path`,
    `statement_timeout`, …) to the server as a session setting; libpq rejects
    them as unknown. They are moved into `options` as `-c key=value`, merged
    after any `options` the URL already has. lib/pq's own client-side
    parameters are dropped.

    Raises ValueError for a malformed URL.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    search_path = next((v for k, v in query if k == "search_path"), None)
    known = _libpq_keywords()
    kept: list[tuple[str, str]] = []
    options: list[str] = []
    settings: list[str] = []
    for key, value in query:
        if key == "options":
            if value:
                options.append(value)
        elif key in _LIB_PQ_CLIENT_ONLY:
            continue
        elif key in known:
            kept.append((key, value))
        else:
            settings.append(f"-c {key}={escape_option_value(value)}")
    if options or settings:
        kept.append(("options", " ".join(options + settings)))
    conninfo = urlunsplit(parts._replace(query=urlencode(kept, quote_via=quote)))
    return conninfo, search_path or None


def search_path_of(store: str) -> str | None:
    """The `search_path` URL parameter, or None (also for a malformed URL)."""
    try:
        query = parse_qsl(urlsplit(store).query, keep_blank_values=True)
    except ValueError:
        return None
    return next((v for k, v in query if k == "search_path"), None) or None


def _url_secrets(store: str) -> set[str]:
    """Every form of the password in `store` (raw, percent-decoded, query value)."""
    parts = urlsplit(store)
    secrets: set[str] = set()
    if parts.password:
        secrets |= {parts.password, unquote(parts.password)}
    for pair in parts.query.split("&"):
        key, sep, raw = pair.partition("=")
        if sep and unquote(key).lower() == "password" and raw:
            secrets |= {raw, unquote(raw), unquote(raw.replace("+", " "))}
    return {s for s in secrets if s}


def _replace_secret(text: str, secret: str) -> str:
    if len(secret) >= _MIN_LITERAL_SECRET_LEN:
        return text.replace(secret, "***")
    # Short: only where it stands alone, e.g. `pq: password p@ss rejected`.
    return re.sub(rf"(?<![\w%]){re.escape(secret)}(?![\w%])", "***", text)


def redact_store(text: str, store: str) -> str:
    """Strip the session store URL and its password from an error message."""
    if not is_postgres_url(store):
        return text
    urls = [store]
    secrets: set[str] = set()
    try:
        urls.append(libpq_conninfo(store)[0])
        secrets = _url_secrets(store)
    except ValueError:
        pass  # malformed: the whole-string passes below still apply
    for url in sorted(set(urls), key=len, reverse=True):
        text = text.replace(url, _STORE_PLACEHOLDER)
    # Longest first so one form never leaves part of another behind.
    for secret in sorted(secrets, key=len, reverse=True):
        text = _replace_secret(text, secret)
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    return _PASSWORD_PARAM_RE.sub("password=***", text)


def describe_store(store: str) -> str:
    """A log-safe description of the session store (never the URL)."""
    if not is_postgres_url(store):
        return f"sqlite {store}"
    try:
        urlsplit(store)
    except ValueError:
        return "postgres (schema=unknown)"
    return f"postgres (schema={search_path_of(store) or 'default'})"


def jid_user(jid: str) -> str:
    """`4912…:12@s.whatsapp.net` (or `4912….1:12@…`) → `4912…`."""
    return jid.partition("@")[0].partition(":")[0].partition(".")[0]


def _jid_device(jid: str) -> int:
    """`4912…:12@…` → 12; no (or a non-numeric) device part → 0."""
    device = jid.partition("@")[0].partition(":")[2]
    return int(device) if device.isascii() and device.isdigit() else 0


def stored_device_jids(store: str) -> list[str]:
    """Every `whatsmeow_device.jid` in the store; a missing file or table means none."""
    if not is_postgres_url(store):
        path = Path(store)
        if not path.is_file():
            return []
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return [row[0] for row in conn.execute("SELECT jid FROM whatsmeow_device")]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise
        finally:
            conn.close()

    try:
        import psycopg
    except ImportError:
        raise RuntimeError(
            "PINCER_WHATSAPP_SESSION_DB is a Postgres URL but psycopg is not installed; "
            "install the `postgres` extra (pip install 'pincer-agent[postgres]')"
        ) from None
    conninfo, _ = libpq_conninfo(store)
    kwargs: dict[str, Any] = {}
    if not any(k == "connect_timeout" for k, _ in parse_qsl(urlsplit(conninfo).query)):
        kwargs["connect_timeout"] = STORE_LOOKUP_CONNECT_TIMEOUT
    try:
        with psycopg.connect(conninfo, **kwargs) as pg:
            return [row[0] for row in pg.execute("SELECT jid FROM whatsmeow_device")]
    except psycopg.errors.UndefinedTable:
        return []


def stored_device_jid(store: str, phone: str) -> str | None:
    """The stored device JID whose user part is `phone`, or None.

    Re-pairing a phone without a logout leaves a stale row beside the live
    one; the highest device number is the most recent pairing.
    """
    matches = [jid for jid in stored_device_jids(store) if jid_user(jid) == phone]
    if len(matches) > 1:
        logger.warning(
            "WhatsApp: %d stored devices for %s; using the most recent (highest device number)",
            len(matches),
            phone,
        )
    return max(matches, key=_jid_device, default=None)


def parse_device_jid(jid: str) -> Any:
    """`user[.agent][:device]@server` → neonize JID proto."""
    user_part, _, server = jid.partition("@")
    user, _, device = user_part.partition(":")
    user, _, agent = user.partition(".")
    parsed = build_jid(user, server or "s.whatsapp.net")
    parsed.Device = int(device or 0)
    parsed.RawAgent = int(agent or 0)
    return parsed
