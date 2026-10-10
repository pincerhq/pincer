"""Credential primitives for per-identity API auth.

Three kinds of secret, each handled the way its entropy allows:

- **Passwords** are human-chosen, so they are hashed with Argon2id
  (`argon2-cffi` defaults: t=3, m=64 MiB, p=4). That is deliberately slow, and
  the process that serves logins also runs real-time voice loops, so hashing
  never touches the event loop: it runs on a two-thread pool of its own. The
  pool is the concurrency limit — login is unauthenticated, and an unbounded
  number of 64 MiB hashes is a memory-exhaustion lever.
- **API keys** are 256 random bits, so a plain SHA-256 is enough and keeps the
  lookup a single indexed equality.
- **Session tokens** are HS256 JWTs. The algorithm is pinned on decode, and a
  token's `typ` is checked against what the caller expects, so a refresh token
  (or a listen-in ticket) is never accepted as an access token.

Nothing here touches the database; `pincer.services.auth` owns the rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY_PREFIX = "pnc_"
#: Characters of a key kept in clear for the masked display (`pnc_Ab3d…wxyz`).
API_KEY_VISIBLE_PREFIX = len(API_KEY_PREFIX) + 4
API_KEY_VISIBLE_SUFFIX = 4

MIN_PASSWORD_LENGTH = 8
#: Argon2 hashes any length, but the request body is attacker-sized.
MAX_PASSWORD_LENGTH = 256

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "pincer"
JWT_AUDIENCE = "pincer-api"
MIN_JWT_SECRET_LENGTH = 32
JWT_SECRET_FILENAME = "jwt_secret"

TokenType = Literal["access", "refresh", "ws"]

_hasher = PasswordHasher()
_argon_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pincer-argon2")


class TokenError(Exception):
    """A session token that must not be accepted."""


class TokenExpiredError(TokenError):
    """A well-formed, correctly signed token past its `exp`."""


class TokenInvalidError(TokenError):
    """Anything else: bad signature, wrong type, wrong audience, garbage."""


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    typ: TokenType
    ver: int
    exp: int
    #: The call a listen-in ticket is bound to (`typ == "ws"` only).
    call: str | None = None


# ── passwords ────────────────────────────────────────────────────────


def validate_password(password: str) -> None:
    """Raise ValueError when `password` is not acceptable as a new password."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters")


async def _in_argon_pool[T](fn: Any, *args: Any) -> T:
    result: T = await asyncio.get_running_loop().run_in_executor(_argon_pool, fn, *args)
    return result


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


@cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def _dummy_verify(password: str) -> bool:
    return _verify(_dummy_hash(), password)


async def hash_password(password: str) -> str:
    """The Argon2id encoded string for `password`."""
    return await _in_argon_pool(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    return await _in_argon_pool(_verify, password_hash, password)


async def dummy_verify(password: str) -> None:
    """Spend one verification on nothing.

    Login runs this for an unknown user or an identity without a password, so
    the response time does not say which names exist.
    """
    await _in_argon_pool(_dummy_verify, password)


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than today's."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (Argon2Error, InvalidHashError):
        return False


# ── API keys ─────────────────────────────────────────────────────────


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def looks_like_api_key(value: str) -> bool:
    return value.startswith(API_KEY_PREFIX)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def api_key_display_parts(api_key: str) -> tuple[str, str]:
    """The (prefix, last4) stored beside the hash for the masked display."""
    return api_key[:API_KEY_VISIBLE_PREFIX], api_key[-API_KEY_VISIBLE_SUFFIX:]


def mask_api_key(prefix: str, last4: str) -> str:
    return f"{prefix}…{last4}"


# ── session tokens ───────────────────────────────────────────────────


def encode_token(
    secret: str,
    *,
    sub: str,
    typ: TokenType,
    ver: int,
    ttl_seconds: int,
    call: str | None = None,
    now: float | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    payload: dict[str, Any] = {
        "sub": sub,
        "typ": typ,
        "ver": ver,
        "iat": issued,
        "exp": issued + ttl_seconds,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        # Two tokens minted in the same second would otherwise be identical.
        "jti": secrets.token_urlsafe(8),
    }
    if call is not None:
        payload["call"] = call
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_token(secret: str, token: str, *, typ: TokenType) -> TokenClaims:
    """Verify `token` and return its claims.

    Raises `TokenExpiredError` for a genuine token past its expiry and
    `TokenInvalidError` for everything else.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["sub", "typ", "ver", "iat", "exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenInvalidError("invalid token") from exc

    sub, ver, call = payload.get("sub"), payload.get("ver"), payload.get("call")
    if payload.get("typ") != typ or not isinstance(sub, str) or not sub:
        raise TokenInvalidError("invalid token")
    if not isinstance(ver, int) or isinstance(ver, bool):
        raise TokenInvalidError("invalid token")
    if call is not None and not isinstance(call, str):
        raise TokenInvalidError("invalid token")
    return TokenClaims(sub=sub, typ=typ, ver=ver, exp=int(payload["exp"]), call=call)


def load_or_create_jwt_secret(configured: str, data_dir: Path) -> str:
    """The HS256 signing secret: `PINCER_JWT_SECRET`, else a generated file.

    The file is created once with mode 0600 under `data_dir` and reused, so
    sessions survive a restart. A deployment with several replicas, or one
    whose data dir is not persistent, must set `PINCER_JWT_SECRET` instead —
    `pincer doctor` checks for that in production.
    """
    if configured:
        if len(configured) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(f"PINCER_JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
        return configured

    path = data_dir / JWT_SECRET_FILENAME
    with contextlib.suppress(FileNotFoundError):
        stored = path.read_text().strip()
        if len(stored) >= MIN_JWT_SECRET_LENGTH:
            return stored
        logger.warning("JWT secret file %s is too short; regenerating", path)
        path.unlink()

    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL: a second process starting at the same moment must not
        # overwrite the secret the first one is already signing with.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as handle:
        handle.write(secret)
    logger.info("Generated a new JWT signing secret at %s", path)
    return secret
