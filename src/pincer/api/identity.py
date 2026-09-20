"""Identity API — expose the cross-channel identity map."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pincer.services.identity import IdentityServiceDep

router = APIRouter(prefix="/api/identity", tags=["identity"])


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
