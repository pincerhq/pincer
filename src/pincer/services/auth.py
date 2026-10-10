"""Authentication of identities against the REST API.

An identity proves itself in one of two ways:

- a **session**: name or email plus password, exchanged for a short-lived
  access JWT and a refresh JWT (the dashboard);
- an **API key**: one per identity, sent as the Bearer value as-is (headless
  consumers such as the web chat widget).

`pincer.security.credentials` has the primitives; this owns the rows and the
rules — who a login identifier names, when a token is stale, when a key may be
replaced.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Depends
from starlette.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves the dependency's annotation

from pincer.db.engine import get_database_url
from pincer.db.ids import new_id
from pincer.db.session import session_scope
from pincer.models.identity import AuthSession
from pincer.repositories.identity import (
    AuthSessionRepository,
    IdentityCredentialRepository,
    IdentityProfileRepository,
)
from pincer.security import credentials as creds
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.models.identity import IdentityCredential

logger = logging.getLogger(__name__)

#: A listen-in ticket only has to survive the WebSocket handshake.
WS_TICKET_TTL_SECONDS = 60

#: How long the refresh token a rotation just replaced is still honoured.
#: Two tabs whose access tokens expire together both refresh with the same
#: token; the slower one is not a thief.
REFRESH_REUSE_GRACE_SECONDS = 30

AuthMethod = Literal["jwt", "api_key"]


class AuthError(Exception):
    """The caller could not be authenticated."""

    #: Machine-readable reason, returned to the client as `error`.
    code = "invalid_token"
    #: Whether this failure spends the caller's brute-force budget.
    counts_as_failure = True


class InvalidCredentialsError(AuthError):
    """Wrong name, email or password. Deliberately says no more than that."""

    code = "invalid_credentials"


class InvalidTokenError(AuthError):
    code = "invalid_token"


class TokenStaleError(AuthError):
    """A genuine token that is expired, or that predates a password change.

    Not a guess — it carries a valid signature — so it must not count toward
    the lockout: the dashboard fires several requests at once, and every one
    of them would fail in the same instant its access token expires.
    """

    code = "token_expired"
    counts_as_failure = False


class UnknownIdentityError(LookupError):
    """No identity with this id."""


class ApiKeyExistsError(Exception):
    """The identity already has an API key and replacing it was not forced."""


@dataclass(frozen=True)
class AuthIdentity:
    """Who a request is, and how they proved it."""

    pincer_user_id: str
    method: AuthMethod
    #: The signed-in session behind an access token; None for an API key.
    session_id: str | None = None

    @property
    def interactive(self) -> bool:
        """A signed-in person, as opposed to a key that may be shared or embedded."""
        return self.method == "jwt"


@dataclass(frozen=True)
class TokenPair:
    pincer_user_id: str
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class ApiKeyInfo:
    masked: str
    created_at: str | None


@dataclass(frozen=True)
class CredentialSummary:
    has_password: bool
    api_key: ApiKeyInfo | None


def account_key(pincer_user_id: str) -> str:
    """The brute-force budget of one identity (see `AuthService.login_account`)."""
    return f"id:{pincer_user_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key_info(row: IdentityCredential | None) -> ApiKeyInfo | None:
    if row is None or not row.api_key_hash:
        return None
    return ApiKeyInfo(
        masked=creds.mask_api_key(row.api_key_prefix or creds.API_KEY_PREFIX, row.api_key_last4 or ""),
        created_at=row.api_key_created_at,
    )


class AuthService(DatabaseService):
    def __init__(
        self,
        url: str | None = None,
        *,
        jwt_secret: str,
        access_ttl_seconds: int = 1800,
        refresh_ttl_seconds: int = 604800,
    ) -> None:
        super().__init__(url)
        self._secret = jwt_secret
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    # ── sessions ─────────────────────────────────────────────────────

    def _pair(self, pincer_user_id: str, token_version: int, session_id: str, refresh_jti: str) -> TokenPair:
        def mint(typ: creds.TokenType, ttl: int, jti: str | None = None) -> str:
            return creds.encode_token(
                self._secret, sub=pincer_user_id, typ=typ, ver=token_version, ttl_seconds=ttl, sid=session_id, jti=jti
            )

        return TokenPair(
            pincer_user_id=pincer_user_id,
            access_token=mint("access", self._access_ttl),
            refresh_token=mint("refresh", self._refresh_ttl, refresh_jti),
            expires_in=self._access_ttl,
        )

    async def _start_session(self, pincer_user_id: str, token_version: int) -> TokenPair:
        """Open a server-side session and mint its first token pair."""
        session_id, refresh_jti, now = new_id(), creds.new_token_id(), time.time()
        async with session_scope(self._url) as session:
            sessions = AuthSessionRepository(session)
            await sessions.delete_expired(pincer_user_id, now=now)
            await sessions.add(
                AuthSession(
                    id=session_id,
                    pincer_user_id=pincer_user_id,
                    refresh_jti=refresh_jti,
                    expires_at=now + self._refresh_ttl,
                ),
                refresh=False,
            )
        return self._pair(pincer_user_id, token_version, session_id, refresh_jti)

    async def _credential(self, pincer_user_id: str) -> IdentityCredential | None:
        async with session_scope(self._url) as session:
            return await IdentityCredentialRepository(session).get(pincer_user_id)

    @staticmethod
    async def _resolve(session: Any, identifier: str) -> str | None:
        """The identity `identifier` names: an exact identity id first, else
        an email. An email several identities share names nobody."""
        profiles = IdentityProfileRepository(session)
        profile = await profiles.get(identifier)
        if profile is None and "@" in identifier:
            matches = await profiles.find_by_email_ci(identifier)
            profile = matches[0] if len(matches) == 1 else None
        return None if profile is None else profile.pincer_user_id

    async def login_account(self, identifier: str) -> str:
        """The key one account's login failures are counted under.

        The identity the identifier resolves to, so a name and an email that
        mean the same person share one budget. An identifier that names
        nobody is its own key — and is throttled exactly like a real one, so
        the lockout does not say which names exist.
        """
        identifier = identifier.strip()
        async with session_scope(self._url) as session:
            resolved = await self._resolve(session, identifier)
        return account_key(resolved) if resolved else f"unknown:{identifier.lower()}"

    async def _credential_for_login(self, identifier: str) -> IdentityCredential | None:
        async with session_scope(self._url) as session:
            resolved = await self._resolve(session, identifier)
            if resolved is None:
                return None
            return await IdentityCredentialRepository(session).get(resolved)

    async def login(self, identifier: str, password: str) -> TokenPair:
        """Exchange a name or email and a password for a token pair.

        Every failure is the same `InvalidCredentialsError` after the same
        amount of hashing, so neither the message nor the timing tells an
        unknown name from a wrong password.
        """
        if len(password) > creds.MAX_PASSWORD_LENGTH:
            # No stored password is this long, and hashing attacker-sized input is not free.
            raise InvalidCredentialsError
        row = await self._credential_for_login(identifier.strip())
        if row is None or not row.password_hash:
            await creds.dummy_verify(password)
            raise InvalidCredentialsError
        if not await creds.verify_password(row.password_hash, password):
            raise InvalidCredentialsError

        if creds.needs_rehash(row.password_hash):
            # The parameters were raised since this hash was made; the clear
            # password is only ever in hand here.
            upgraded = await creds.hash_password(password)
            async with session_scope(self._url) as session:
                await IdentityCredentialRepository(session).replace_password_hash(row.pincer_user_id, upgraded)
        return await self._start_session(row.pincer_user_id, row.token_version)

    async def _current(self, claims: creds.TokenClaims) -> IdentityCredential:
        """The credentials a token was issued under, if it is still current."""
        row = await self._credential(claims.sub)
        if row is None or row.token_version != claims.ver:
            raise TokenStaleError
        return row

    def _decode(self, token: str, typ: creds.TokenType) -> creds.TokenClaims:
        try:
            return creds.decode_token(self._secret, token, typ=typ)
        except creds.TokenExpiredError as exc:
            raise TokenStaleError from exc
        except creds.TokenError as exc:
            raise InvalidTokenError from exc

    async def _live_session(self, sessions: AuthSessionRepository, claims: creds.TokenClaims) -> AuthSession:
        """The session a token belongs to, if it has not been signed out."""
        row = await sessions.get(claims.sid) if claims.sid else None
        if row is None or row.pincer_user_id != claims.sub or row.expires_at < time.time():
            raise TokenStaleError
        return row

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair. The token is spent: only
        the one in the returned pair refreshes this session from now on.

        A refresh token that was already replaced is either a copy someone
        kept or a race the grace window did not cover. Either way the session
        is ended, so whoever holds the stolen copy is cut off with it.
        """
        claims = self._decode(refresh_token, "refresh")
        credential = await self._current(claims)
        now, new_jti = time.time(), creds.new_token_id()
        reused = False
        async with session_scope(self._url) as session:
            sessions = AuthSessionRepository(session)
            row = await self._live_session(sessions, claims)
            rotation: dict[str, Any] = {"refresh_jti": new_jti, "expires_at": now + self._refresh_ttl}
            if claims.jti == row.refresh_jti:
                rotation |= {"prev_refresh_jti": row.refresh_jti, "rotated_at": now}
            elif not (
                claims.jti == row.prev_refresh_jti
                and row.rotated_at is not None
                and now - row.rotated_at <= REFRESH_REUSE_GRACE_SECONDS
            ):
                reused = True
            if reused:
                await sessions.delete_by_id(row.id)
            else:
                await sessions.rotate(row.id, rotation)
        if reused:
            logger.warning("Refresh token reuse for %s: session %s ended", claims.sub, claims.sid)
            raise TokenStaleError
        return self._pair(credential.pincer_user_id, credential.token_version, claims.sid or "", new_jti)

    async def authenticate_bearer(self, value: str) -> AuthIdentity:
        """Resolve a Bearer value — an API key or an access token — to its identity."""
        if not value:
            raise InvalidTokenError
        if creds.looks_like_api_key(value):
            async with session_scope(self._url) as session:
                row = await IdentityCredentialRepository(session).by_api_key_hash(creds.hash_api_key(value))
            if row is None:
                raise InvalidTokenError
            return AuthIdentity(row.pincer_user_id, "api_key")
        claims = self._decode(value, "access")
        async with session_scope(self._url) as session:
            credential = await IdentityCredentialRepository(session).get(claims.sub)
            if credential is None or credential.token_version != claims.ver:
                raise TokenStaleError
            live = await self._live_session(AuthSessionRepository(session), claims)
        return AuthIdentity(credential.pincer_user_id, "jwt", session_id=live.id)

    async def logout(self, identity: AuthIdentity) -> None:
        """End the session this identity is signed in with. Its access and
        refresh tokens stop working at once; other sessions are untouched."""
        if identity.session_id is None:
            return
        async with session_scope(self._url) as session:
            await AuthSessionRepository(session).delete_by_id(identity.session_id)

    # ── passwords ────────────────────────────────────────────────────

    async def set_password(self, pincer_user_id: str, password: str) -> None:
        """Set an identity's password without asking for the old one (the CLI).

        Raises `UnknownIdentityError` for an id with no profile and
        `ValueError` for a password that is not acceptable. Signs the identity
        out everywhere.
        """
        creds.validate_password(password)
        async with session_scope(self._url) as session:
            if await IdentityProfileRepository(session).get(pincer_user_id) is None:
                raise UnknownIdentityError(pincer_user_id)
        password_hash = await creds.hash_password(password)
        async with session_scope(self._url) as session:
            await IdentityCredentialRepository(session).set_password(pincer_user_id, password_hash, now=_now())
            await AuthSessionRepository(session).delete_for_user(pincer_user_id)

    async def revoke(self, pincer_user_id: str) -> bool:
        """Take away everything an identity signs in with: password, API key
        and every session. False when it had nothing."""
        async with session_scope(self._url) as session:
            await AuthSessionRepository(session).delete_for_user(pincer_user_id)
            return bool(await IdentityCredentialRepository(session).delete_by_id(pincer_user_id))

    async def change_password(self, pincer_user_id: str, current_password: str, new_password: str) -> TokenPair:
        """Change one's own password. Returns the pair that replaces the
        caller's session, since every token issued before now is stale."""
        if len(current_password) > creds.MAX_PASSWORD_LENGTH:
            raise InvalidCredentialsError
        row = await self._credential(pincer_user_id)
        if row is None or not row.password_hash:
            await creds.dummy_verify(current_password)
            raise InvalidCredentialsError
        if not await creds.verify_password(row.password_hash, current_password):
            raise InvalidCredentialsError
        await self.set_password(pincer_user_id, new_password)
        fresh = await self._credential(pincer_user_id)
        return await self._start_session(pincer_user_id, fresh.token_version if fresh else row.token_version + 1)

    # ── API keys ─────────────────────────────────────────────────────

    async def api_key_info(self, pincer_user_id: str) -> ApiKeyInfo | None:
        return _key_info(await self._credential(pincer_user_id))

    async def generate_api_key(self, pincer_user_id: str, *, force: bool = False) -> tuple[str, ApiKeyInfo]:
        """Mint the identity's API key and return it in clear — the only time
        it ever is. Replacing an existing key needs `force`, because the old
        one stops working at once.
        """
        api_key = creds.new_api_key()
        prefix, last4 = creds.api_key_display_parts(api_key)
        now = _now()
        async with session_scope(self._url) as session:
            if await IdentityProfileRepository(session).get(pincer_user_id) is None:
                raise UnknownIdentityError(pincer_user_id)
            credentials = IdentityCredentialRepository(session)
            existing = await credentials.get(pincer_user_id)
            if existing is not None and existing.api_key_hash and not force:
                raise ApiKeyExistsError(pincer_user_id)
            await credentials.set_api_key(
                pincer_user_id, api_key_hash=creds.hash_api_key(api_key), prefix=prefix, last4=last4, now=now
            )
        return api_key, ApiKeyInfo(masked=creds.mask_api_key(prefix, last4), created_at=now)

    # ── listen-in tickets ────────────────────────────────────────────

    async def issue_ws_ticket(self, pincer_user_id: str, call_sid: str) -> str:
        """A one-minute ticket for one call's listen-in WebSocket.

        A browser cannot set headers on a WebSocket, and a session token in
        the query string would sit in every proxy's access log for as long as
        it is valid. This is good for one call and one minute.
        """
        row = await self._credential(pincer_user_id)
        if row is None:
            raise UnknownIdentityError(pincer_user_id)
        return creds.encode_token(
            self._secret,
            sub=pincer_user_id,
            typ="ws",
            ver=row.token_version,
            ttl_seconds=WS_TICKET_TTL_SECONDS,
            call=call_sid,
        )

    async def authenticate_ws_ticket(self, ticket: str, call_sid: str) -> AuthIdentity:
        claims = self._decode(ticket, "ws")
        if claims.call != call_sid:
            raise InvalidTokenError
        row = await self._current(claims)
        return AuthIdentity(row.pincer_user_id, "jwt")

    # ── health ───────────────────────────────────────────────────────

    async def credential_summaries(self) -> dict[str, CredentialSummary]:
        """What each identity can sign in with, by id (for `pincer identity list`)."""
        async with session_scope(self._url) as session:
            rows = await IdentityCredentialRepository(session).list()
        return {
            row.pincer_user_id: CredentialSummary(has_password=bool(row.password_hash), api_key=_key_info(row))
            for row in rows
        }

    async def identities_with_credentials(self) -> int:
        """How many identities can authenticate at all (for `pincer doctor`)."""
        async with session_scope(self._url) as session:
            return await IdentityCredentialRepository(session).count_usable()


def count_identities_with_credentials(db_path: Path) -> int | None:
    """How many identities can sign in, for the synchronous health checks
    (`pincer doctor`, the pilot preflight). None when there is no database yet.

    Those checks also run inside a running loop (`GET /api/doctor`), where
    `asyncio.run` refuses to start, so the query gets a loop of its own on a
    worker thread there.
    """
    import asyncio
    import concurrent.futures

    from pincer.db.engine import _database_file, dispose_engines, get_sync_url

    # Opening a SQLite file that isn't there creates it; a fresh install has
    # no identities, so don't leave an empty database behind.
    database = _database_file(get_sync_url(db_path))
    if database is not None and not database.exists():
        return None

    async def count() -> int:
        try:
            async with session_scope(get_database_url(db_path)) as session:
                return await IdentityCredentialRepository(session).count_usable()
        finally:
            await dispose_engines()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(count())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, count()).result()


@lru_cache(maxsize=8)
def _jwt_secret(configured: str, data_dir: Path) -> str:
    return creds.load_or_create_jwt_secret(configured, data_dir)


def build_auth_service() -> AuthService:
    """The auth service on the configured database and signing secret."""
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    return AuthService(
        get_database_url(settings.db_path),
        jwt_secret=_jwt_secret(settings.jwt_secret.get_secret_value(), settings.data_dir),
        access_ttl_seconds=settings.jwt_access_ttl_seconds,
        refresh_ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )


def auth_service_for(connection: HTTPConnection) -> AuthService:
    """The app's auth service, built on first use and then kept.

    Building one reads the settings and the signing secret; the middleware
    needs it on every request, on the same event loop as the voice audio.
    """
    state: Any = connection.app.state
    service: AuthService | None = getattr(state, "auth_service", None)
    if service is None:
        service = state.auth_service = build_auth_service()
    return service


async def get_auth_service(connection: HTTPConnection) -> AuthService:
    """FastAPI dependency."""
    return auth_service_for(connection)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
