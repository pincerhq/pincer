"""Auth API — sign in, refresh a session, and resolve who a request is.

`POST /api/auth/login` and `/refresh` are the only credential-bearing routes
that are public, so they carry their own brute-force guards: the per-IP one
shared with the middleware, and a per-account one — the client IP comes from
an unverified `X-Forwarded-For`, so it alone would let one attacker rotate
through addresses against a single account.

The rest of this module is what every other router uses: `CurrentIdentity`
for the caller the middleware resolved, and `require_interactive` for the
actions an API key must not perform.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from pincer.api.auth_guard import AuthGuard, audit_auth_failure, client_ip
from pincer.services.auth import (
    AuthError,
    AuthIdentity,
    AuthService,
    AuthServiceDep,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenPair,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_NO_STORE = {"Cache-Control": "no-store"}

_MESSAGES = {
    "invalid_credentials": "Invalid credentials",
    "invalid_token": "Invalid token",
    "token_expired": "Token expired",
    "locked_out": "Too many failed authentication attempts",
}


class LoginIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=320, description="Identity name or email")
    # No max_length here: a validation error echoes the offending input back,
    # and this input is a password. The service caps the length instead.
    password: SecretStr = Field(min_length=1)


class RefreshIn(BaseModel):
    refresh_token: SecretStr = Field(min_length=1, max_length=4096)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    pincer_user_id: str

    @classmethod
    def of(cls, pair: TokenPair) -> TokenPairOut:
        return cls(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
            pincer_user_id=pair.pincer_user_id,
        )


# ── shared by the middleware and the routers ─────────────────────────


def auth_error_response(code: str, *, status_code: int = 401, retry_after: int = 0) -> JSONResponse:
    """The one shape every authentication failure has: `error` is the code a
    client branches on, `detail` the sentence a person reads."""
    headers: dict[str, str] = {}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status_code, content={"error": code, "detail": _MESSAGES.get(code, code)}, headers=headers
    )


def bearer_value(connection: HTTPConnection) -> str:
    """The credential in the Authorization header, or "" when there is none."""
    scheme, _, value = connection.headers.get("Authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def authenticate_connection(connection: HTTPConnection, auth: AuthService) -> AuthIdentity:
    """Who this HTTP request or WebSocket is, from its Authorization header.

    Raises `AuthError`; the caller decides what a failure costs.
    """
    value = bearer_value(connection)
    if not value:
        raise InvalidTokenError
    return await auth.authenticate_bearer(value)


def current_identity(request: Request) -> AuthIdentity:
    identity: AuthIdentity | None = getattr(request.state, "identity", None)
    if identity is None:
        # Only reachable with PINCER_AUTH_DISABLED: the middleware let the
        # request through without knowing who sent it.
        raise HTTPException(status_code=401, detail="No authenticated identity")
    return identity


CurrentIdentity = Annotated[AuthIdentity, Depends(current_identity)]


def optional_identity(request: Request) -> AuthIdentity | None:
    identity: AuthIdentity | None = getattr(request.state, "identity", None)
    return identity


OptionalIdentity = Annotated[AuthIdentity | None, Depends(optional_identity)]


def require_interactive(identity: CurrentIdentity) -> AuthIdentity:
    """Only a signed-in session may do this. An API key is long-lived and
    often embedded somewhere, so it can neither change the password nor mint
    its own replacement."""
    if not identity.interactive:
        raise HTTPException(status_code=403, detail="This action requires a signed-in session, not an API key")
    return identity


InteractiveIdentity = Annotated[AuthIdentity, Depends(require_interactive)]


def guards(connection: HTTPConnection) -> tuple[AuthGuard, AuthGuard]:
    """The app's (per-IP, per-account) brute-force guards."""
    state: Any = connection.app.state
    return state.auth_guard, state.login_guard


# ── routes ───────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenPairOut)
async def login(body: LoginIn, request: Request, auth: AuthServiceDep) -> JSONResponse:
    """Exchange an identity name or email and a password for a token pair."""
    ip_guard, account_guard = guards(request)
    ip = client_ip(request)
    # Not the raw identifier: `Alice` and `alice@Example.com ` must not each
    # get a failure budget of their own.
    account = body.identifier.strip().lower()

    wait = max(ip_guard.retry_after(ip), account_guard.retry_after(account))
    if wait:
        await audit_auth_failure(ip, request.url.path, "locked_out", locked_for=wait)
        return auth_error_response("locked_out", status_code=429, retry_after=wait)

    try:
        pair = await auth.login(body.identifier, body.password.get_secret_value())
    except InvalidCredentialsError as exc:
        locked_for = max(ip_guard.record_failure(ip), account_guard.record_failure(account))
        await audit_auth_failure(ip, request.url.path, exc.code, locked_for=locked_for)
        return auth_error_response(exc.code)

    ip_guard.record_success(ip)
    account_guard.record_success(account)
    return JSONResponse(TokenPairOut.of(pair).model_dump(), headers=_NO_STORE)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshIn, request: Request, auth: AuthServiceDep) -> JSONResponse:
    """Exchange a refresh token for a new token pair."""
    ip_guard, _ = guards(request)
    ip = client_ip(request)
    wait = ip_guard.retry_after(ip)
    if wait:
        await audit_auth_failure(ip, request.url.path, "locked_out", locked_for=wait)
        return auth_error_response("locked_out", status_code=429, retry_after=wait)

    try:
        pair = await auth.refresh(body.refresh_token.get_secret_value())
    except AuthError as exc:
        if exc.counts_as_failure:
            locked_for = ip_guard.record_failure(ip)
            await audit_auth_failure(ip, request.url.path, exc.code, locked_for=locked_for)
        return auth_error_response(exc.code)
    return JSONResponse(TokenPairOut.of(pair).model_dump(), headers=_NO_STORE)
