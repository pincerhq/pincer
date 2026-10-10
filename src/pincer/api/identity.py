"""Identity API — the cross-channel identity map, and the caller's own account.

The `/me` routes are declared before `/{pincer_user_id}` so they are matched
first; `me` is a reserved identity name for the same reason.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_serializer

from pincer.api.auth import CurrentIdentity, InteractiveIdentity, TokenPairOut, guards
from pincer.services.auth import (
    ApiKeyExistsError,
    AuthServiceDep,
    InvalidCredentialsError,
    UnknownIdentityError,
    account_key,
)
from pincer.services.identity import IdentityServiceDep

router = APIRouter(prefix="/api/identity", tags=["identity"])


class PasswordChangeIn(BaseModel):
    # Length limits live in the service: a 422 would echo the password back.
    current_password: SecretStr = Field(min_length=1)
    new_password: SecretStr = Field(min_length=1)


class ApiKeyOut(BaseModel):
    exists: bool
    masked: str | None = None
    created_at: str | None = None


class ApiKeyCreatedOut(BaseModel):
    """The one response that carries an API key in clear."""

    api_key: SecretStr
    masked: str
    created_at: str | None = None

    @field_serializer("api_key", when_used="json")
    def _reveal(self, value: SecretStr) -> str:
        return value.get_secret_value()


@router.get("")
async def list_identities(
    identities: IdentityServiceDep,
    limit: int = Query(default=100, ge=1, le=1000),
    search: str | None = Query(default=None, description="Filter by pincer_user_id or channel_user_id"),
) -> dict[str, Any]:
    """Return all identities from the database with their linked channels."""
    try:
        rows = await identities.list_profiles(search=search, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error: {exc}") from exc
    return {"identities": rows, "total": len(rows)}


@router.get("/me")
async def get_me(identity: CurrentIdentity, identities: IdentityServiceDep) -> dict[str, Any]:
    """The identity this request is authenticated as."""
    profile = await identities.profile_with_channels(identity.pincer_user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    return {**profile, "auth_method": identity.method}


@router.put("/me/password", response_model=TokenPairOut)
async def change_my_password(
    body: PasswordChangeIn,
    request: Request,
    response: Response,
    identity: InteractiveIdentity,
    auth: AuthServiceDep,
) -> TokenPairOut:
    """Change the caller's password.

    Every token issued before the change stops working — including the one
    this request was made with — so the response carries the pair that
    replaces it.
    """
    # A stolen session must not become a free oracle for the password it
    # does not know: wrong guesses spend the account's login budget.
    _, account_guard = guards(request)
    account = account_key(identity.pincer_user_id)
    wait = account_guard.retry_after(account)
    if wait:
        raise HTTPException(
            status_code=429, detail="Too many failed authentication attempts", headers={"Retry-After": str(wait)}
        )
    try:
        pair = await auth.change_password(
            identity.pincer_user_id,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
    except InvalidCredentialsError as exc:
        account_guard.record_failure(account)
        raise HTTPException(status_code=403, detail="Current password is incorrect") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account_guard.record_success(account)
    response.headers["Cache-Control"] = "no-store"
    return TokenPairOut.of(pair)


@router.get("/me/api-key", response_model=ApiKeyOut)
async def get_my_api_key(identity: CurrentIdentity, auth: AuthServiceDep) -> ApiKeyOut:
    """Whether the caller has an API key, and its masked form. Never the key."""
    info = await auth.api_key_info(identity.pincer_user_id)
    if info is None:
        return ApiKeyOut(exists=False)
    return ApiKeyOut(exists=True, masked=info.masked, created_at=info.created_at)


@router.post("/me/api-key", response_model=ApiKeyCreatedOut, status_code=201)
async def create_my_api_key(
    response: Response,
    identity: InteractiveIdentity,
    auth: AuthServiceDep,
    force: bool = Query(default=False, description="Replace an existing key; the old one stops working at once"),
) -> ApiKeyCreatedOut:
    """Generate the caller's API key. This response is the only time it is shown."""
    try:
        api_key, info = await auth.generate_api_key(identity.pincer_user_id, force=force)
    except ApiKeyExistsError as exc:
        raise HTTPException(
            status_code=409, detail="An API key already exists. Pass force=true to replace it."
        ) from exc
    except UnknownIdentityError as exc:
        raise HTTPException(status_code=404, detail="Identity not found") from exc
    response.headers["Cache-Control"] = "no-store"
    return ApiKeyCreatedOut(api_key=SecretStr(api_key), masked=info.masked, created_at=info.created_at)


@router.get("/{pincer_user_id}")
async def get_identity(pincer_user_id: str, identities: IdentityServiceDep) -> dict[str, Any]:
    """Return a single identity with all linked channels."""
    try:
        identity = await identities.profile_with_channels(pincer_user_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error: {exc}") from exc
    if identity is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    return identity
