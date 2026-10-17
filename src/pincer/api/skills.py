"""Skills API — FastAPI endpoints for discovered SKILL.md skills."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

if TYPE_CHECKING:
    from pincer.tools.skills.index import SkillIndex

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _build_index() -> SkillIndex:
    """The process-wide skill index (shared with the agent when served by `pincer run`).

    Rescanned when it is older than skills_index_cache_ttl_seconds (at most 60),
    and immediately after any index change in any process.
    """
    from pincer.config import get_settings_relaxed
    from pincer.services.skills import get_skill_service

    settings = get_settings_relaxed()
    return get_skill_service(settings).ensure_current(max_age=settings.skills_index_cache_ttl_seconds)


def _skill_entries() -> list[dict[str, Any]]:
    """Discover SKILL.md-based skills via the shared SkillIndex (bundled + user, uniformly)."""
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "status": "active",
            "source": "file",
            "root": entry.skill.source_root,
            # Directory name on disk — GET /api/skills/{name} matches on this, not
            # the frontmatter `name` above, so clients must link using this field.
            "dir": entry.skill.path.name,
        }
        for entry in _build_index().all_skills()
    ]


def _skill_detail(dir_name: str) -> dict[str, Any] | None:
    """Full detail for a single skill, looked up by its directory name on disk.

    Matched against the actual folder name rather than SkillIndex's internal
    (frontmatter-name-keyed) lookup, so this stays correct even if a skill's
    SKILL.md `name` field drifts from its directory name.
    """
    for entry in _build_index().all_skills():
        if entry.skill.path.name == dir_name:
            return {
                "name": entry.name,
                "description": entry.description,
                "status": "active",
                "source": "file",
                "root": entry.skill.source_root,
                "dir": entry.skill.path.name,
                "body": entry.skill.body,
                "files": entry.list_files(),
            }
    return None


@router.get("")
async def list_skills() -> dict[str, list[dict[str, Any]]]:
    """List discovered SKILL.md skills (bundled + user)."""
    return {"skills": await asyncio.to_thread(_skill_entries)}


@router.get("/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    """Get a single skill's full SKILL.md body and file manifest, by directory name."""
    detail = await asyncio.to_thread(_skill_detail, name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {name}")
    return detail
