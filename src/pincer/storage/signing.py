"""Presigned links for local storage roots.

A cloud root's links are signed by the provider. A local root has none, so
Pincer signs its own: an HMAC token over the key, the expiry and the
disposition, served by the public `/api/storage/signed/{token}` route. The
token is the credential.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Literal

from pincer.storage.errors import InvalidSignature, LinkExpired
from pincer.storage.keys import b64decode, b64encode

if TYPE_CHECKING:
    from pincer.config import Settings

type Disposition = Literal["inline", "attachment"]


def signing_key(settings: Settings) -> bytes:
    """The key for local presigned links, derived from the JWT secret so a
    link stays valid across restarts and replicas that share that secret."""
    from pincer.security.credentials import load_or_create_jwt_secret

    secret = load_or_create_jwt_secret(settings.jwt_secret.get_secret_value(), settings.data_dir)
    return hmac.new(secret.encode(), b"pincer-storage-presign", hashlib.sha256).digest()


def _signature(key: bytes, payload: str) -> str:
    return b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def sign(key: bytes, storage_key: str, expires: int, disposition: Disposition) -> str:
    payload = b64encode(json.dumps({"k": storage_key, "e": expires, "d": disposition}, separators=(",", ":")).encode())
    return f"{payload}.{_signature(key, payload)}"


def verify(key: bytes, token: str) -> tuple[str, Disposition]:
    """The storage key and disposition a token made by `sign` grants."""
    payload, _, signature = token.partition(".")
    # Bytes, not str: compare_digest refuses a str with non-ASCII characters,
    # and anyone can put one in this public route's URL.
    if not signature or not hmac.compare_digest(signature.encode(), _signature(key, payload).encode()):
        raise InvalidSignature("Invalid link")
    try:
        claims = json.loads(b64decode(payload))
        storage_key, expires, disposition = str(claims["k"]), int(claims["e"]), claims["d"]
    except (ValueError, KeyError, TypeError, binascii.Error) as e:
        raise InvalidSignature("Invalid link") from e
    if expires < time.time():
        raise LinkExpired("This link has expired")
    if disposition not in ("inline", "attachment"):
        raise InvalidSignature("Invalid link")
    return storage_key, disposition
