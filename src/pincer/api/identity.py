"""Identity API — expose the cross-channel identity map stored in SQLite."""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from pincer.config import get_settings_relaxed

router = APIRouter(prefix="/api/identity", tags=["identity"])


@router.get("")
async def list_identities(
    limit: int = Query(default=100, ge=1, le=1000),
    search: str | None = Query(default=None, description="Filter by pincer_user_id or channel_user_id"),
) -> dict[str, Any]:
    """Return all identities from the database with their linked channels."""
    try:
        settings = get_settings_relaxed()
        db_path = settings.db_path
    except Exception:
        return {"identities": [], "total": 0}

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            # Check that the new schema exists
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='identity_meta'") as cur:
                if not await cur.fetchone():
                    return {"identities": [], "total": 0}

            # Fetch matching identities
            if search:
                async with db.execute(
                    """
                    SELECT DISTINCT m.pincer_user_id, m.preferred_channel, m.display_name, m.created_at,
                        m.active_channel, m.active_channel_updated_at, m.timezone, m.email
                    FROM identity_meta m
                    LEFT JOIN channel_identities ci ON ci.pincer_user_id = m.pincer_user_id
                    WHERE m.pincer_user_id LIKE ? OR ci.channel_user_id LIKE ?
                    ORDER BY m.created_at
                    LIMIT ?
                    """,
                    (f"%{search}%", f"%{search}%", limit),
                ) as cur:
                    meta_rows = await cur.fetchall()
            else:
                async with db.execute(
                    """
                    SELECT pincer_user_id, preferred_channel, display_name, created_at,
                        active_channel, active_channel_updated_at, timezone, email
                    FROM identity_meta
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (limit,),
                ) as cur:
                    meta_rows = await cur.fetchall()

            identities: list[dict[str, Any]] = []
            for meta in meta_rows:
                uid = meta["pincer_user_id"]
                async with db.execute(
                    """
                    SELECT channel, channel_user_id, created_at
                    FROM channel_identities
                    WHERE pincer_user_id = ?
                    ORDER BY created_at
                    """,
                    (uid,),
                ) as cur:
                    channels = [
                        {
                            "channel": row["channel"],
                            "channel_user_id": row["channel_user_id"],
                            "linked_at": row["created_at"],
                        }
                        async for row in cur
                    ]

                identities.append(
                    {
                        "pincer_user_id": uid,
                        "preferred_channel": meta["preferred_channel"],
                        "active_channel": meta["active_channel"],
                        "active_channel_updated_at": meta["active_channel_updated_at"],
                        "display_name": meta["display_name"],
                        "timezone": meta["timezone"],
                        "email": meta["email"],
                        "created_at": meta["created_at"],
                        "channels": channels,
                    }
                )

    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error: {exc}") from exc

    return {"identities": identities, "total": len(identities)}


@router.get("/{pincer_user_id}")
async def get_identity(pincer_user_id: str) -> dict[str, Any]:
    """Return a single identity with all linked channels."""
    try:
        settings = get_settings_relaxed()
        db_path = settings.db_path
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable") from None

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT pincer_user_id, preferred_channel, display_name, created_at, "
                "active_channel, active_channel_updated_at, timezone, email "
                "FROM identity_meta WHERE pincer_user_id = ?",
                (pincer_user_id,),
            ) as cur:
                meta = await cur.fetchone()

            if not meta:
                raise HTTPException(status_code=404, detail="Identity not found")

            async with db.execute(
                """
                SELECT channel, channel_user_id, created_at
                FROM channel_identities
                WHERE pincer_user_id = ?
                ORDER BY created_at
                """,
                (pincer_user_id,),
            ) as cur:
                channels = [
                    {
                        "channel": row["channel"],
                        "channel_user_id": row["channel_user_id"],
                        "linked_at": row["created_at"],
                    }
                    async for row in cur
                ]

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error: {exc}") from exc

    return {
        "pincer_user_id": meta["pincer_user_id"],
        "preferred_channel": meta["preferred_channel"],
        "active_channel": meta["active_channel"],
        "active_channel_updated_at": meta["active_channel_updated_at"],
        "display_name": meta["display_name"],
        "timezone": meta["timezone"],
        "email": meta["email"],
        "created_at": meta["created_at"],
        "channels": channels,
    }
