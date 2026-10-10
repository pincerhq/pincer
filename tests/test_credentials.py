"""Credential primitives: Argon2 passwords, API keys, session JWTs, the signing secret."""

from __future__ import annotations

import stat
import time
from typing import TYPE_CHECKING

import jwt
import pytest

from pincer.security import credentials as creds

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "s" * 64


# ── passwords ────────────────────────────────────────────────────────


async def test_password_hash_is_argon2id_and_verifies() -> None:
    hashed = await creds.hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert await creds.verify_password(hashed, "correct horse battery")
    assert not await creds.verify_password(hashed, "wrong horse battery")
    assert not creds.needs_rehash(hashed)


async def test_verify_rejects_a_malformed_hash_instead_of_raising() -> None:
    assert not await creds.verify_password("not-a-hash", "anything")
    assert not creds.needs_rehash("not-a-hash")


async def test_dummy_verify_accepts_any_input() -> None:
    await creds.dummy_verify("whatever")


@pytest.mark.parametrize("password", ["", "short", "x" * (creds.MAX_PASSWORD_LENGTH + 1)])
def test_validate_password_rejects(password: str) -> None:
    with pytest.raises(ValueError, match="Password must be"):
        creds.validate_password(password)


def test_validate_password_accepts_minimum_length() -> None:
    creds.validate_password("x" * creds.MIN_PASSWORD_LENGTH)


# ── API keys ─────────────────────────────────────────────────────────


def test_api_key_shape_hash_and_mask() -> None:
    key = creds.new_api_key()
    assert creds.looks_like_api_key(key)
    assert key != creds.new_api_key()
    assert len(key) > 40

    digest = creds.hash_api_key(key)
    assert len(digest) == 64
    assert digest == creds.hash_api_key(key)

    prefix, last4 = creds.api_key_display_parts(key)
    masked = creds.mask_api_key(prefix, last4)
    assert masked.startswith("pnc_")
    assert masked.endswith(key[-4:])
    assert key[8:-4] not in masked


def test_a_jwt_does_not_look_like_an_api_key() -> None:
    token = creds.encode_token(SECRET, sub="alice", typ="access", ver=0, ttl_seconds=60)
    assert not creds.looks_like_api_key(token)


# ── session tokens ───────────────────────────────────────────────────


def test_token_round_trip_carries_claims() -> None:
    token = creds.encode_token(SECRET, sub="alice", typ="access", ver=3, ttl_seconds=60)
    claims = creds.decode_token(SECRET, token, typ="access")
    assert (claims.sub, claims.typ, claims.ver, claims.call) == ("alice", "access", 3, None)
    assert claims.exp > time.time()


def test_tokens_minted_in_the_same_second_differ() -> None:
    kwargs = {"sub": "alice", "typ": "access", "ver": 0, "ttl_seconds": 60, "now": 1_900_000_000}
    assert creds.encode_token(SECRET, **kwargs) != creds.encode_token(SECRET, **kwargs)  # type: ignore[arg-type]


def test_refresh_token_is_rejected_as_access_token() -> None:
    refresh = creds.encode_token(SECRET, sub="alice", typ="refresh", ver=0, ttl_seconds=60)
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, refresh, typ="access")


def test_ws_ticket_carries_its_call() -> None:
    ticket = creds.encode_token(SECRET, sub="alice", typ="ws", ver=0, ttl_seconds=60, call="CA123")
    assert creds.decode_token(SECRET, ticket, typ="ws").call == "CA123"


def test_expired_token_raises_expired_not_invalid() -> None:
    token = creds.encode_token(SECRET, sub="alice", typ="access", ver=0, ttl_seconds=60, now=time.time() - 3600)
    with pytest.raises(creds.TokenExpiredError):
        creds.decode_token(SECRET, token, typ="access")


def test_wrong_secret_is_invalid() -> None:
    token = creds.encode_token(SECRET, sub="alice", typ="access", ver=0, ttl_seconds=60)
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token("t" * 64, token, typ="access")


def _forge(payload: dict[str, object], algorithm: str = "HS256", key: str = SECRET) -> str:
    base = {
        "sub": "alice",
        "typ": "access",
        "ver": 0,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "iss": creds.JWT_ISSUER,
        "aud": creds.JWT_AUDIENCE,
    }
    return jwt.encode({**base, **payload}, key, algorithm=algorithm)


@pytest.mark.parametrize(
    "payload",
    [
        {"aud": "someone-else"},
        {"iss": "someone-else"},
        {"ver": "0"},
        {"ver": True},
        {"sub": ""},
        {"call": 5},
    ],
)
def test_malformed_claims_are_invalid(payload: dict[str, object]) -> None:
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, _forge(payload), typ="access")


def test_missing_required_claim_is_invalid() -> None:
    token = jwt.encode({"sub": "alice", "typ": "access", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, token, typ="access")


def test_algorithm_is_pinned() -> None:
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, _forge({}, algorithm="HS512"), typ="access")
    unsigned = jwt.encode({"sub": "alice", "typ": "access", "ver": 0}, "", algorithm="none")
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, unsigned, typ="access")


def test_garbage_is_invalid() -> None:
    with pytest.raises(creds.TokenInvalidError):
        creds.decode_token(SECRET, "not.a.jwt", typ="access")


# ── signing secret ───────────────────────────────────────────────────


def test_configured_secret_wins_and_writes_no_file(tmp_path: Path) -> None:
    assert creds.load_or_create_jwt_secret(SECRET, tmp_path) == SECRET
    assert not (tmp_path / creds.JWT_SECRET_FILENAME).exists()


def test_short_configured_secret_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        creds.load_or_create_jwt_secret("too-short", tmp_path)


def test_secret_file_is_created_0600_and_reused(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"
    first = creds.load_or_create_jwt_secret("", data_dir)
    path = data_dir / creds.JWT_SECRET_FILENAME
    assert len(first) >= creds.MIN_JWT_SECRET_LENGTH
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert creds.load_or_create_jwt_secret("", data_dir) == first


def test_truncated_secret_file_is_regenerated(tmp_path: Path) -> None:
    path = tmp_path / creds.JWT_SECRET_FILENAME
    path.write_text("short")
    secret = creds.load_or_create_jwt_secret("", tmp_path)
    assert len(secret) >= creds.MIN_JWT_SECRET_LENGTH
    assert path.read_text() == secret
