"""Skills API — FastAPI endpoints for discovered SKILL.md skills."""

from __future__ import annotations

import asyncio
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

if TYPE_CHECKING:
    from pincer.tools.skills.index import SkillIndex

router = APIRouter(prefix="/api/skills", tags=["skills"])

# Scanning a remote skills root costs several object-store round-trips, so a
# built index is reused for skills_index_cache_ttl_seconds. Keyed on the inputs
# that shape discovery, so a changed root or cap is never served stale.
_index_cache: tuple[tuple[str, str, int], float, SkillIndex] | None = None
_index_cache_lock = threading.Lock()


def _reset_index_cache() -> None:
    global _index_cache
    with _index_cache_lock:
        _index_cache = None


def _build_index() -> SkillIndex:
    global _index_cache
    from pincer.config import get_settings_relaxed
    from pincer.tools.skills.index import BUNDLED_SKILLS_DIR, SkillIndex
    from pincer.tools.skills.storage import open_user_skills_root

    settings = get_settings_relaxed()
    ttl = settings.skills_index_cache_ttl_seconds
    key = (str(BUNDLED_SKILLS_DIR), str(settings.skills_dir), settings.skills_max_loaded_per_root)
    with _index_cache_lock:
        if ttl > 0 and _index_cache is not None:
            cached_key, built_at, cached = _index_cache
            if cached_key == key and monotonic() - built_at < ttl:
                return cached

        index = SkillIndex(
            bundled_dir=BUNDLED_SKILLS_DIR,
            user_dir=open_user_skills_root(settings),
            max_per_root=settings.skills_max_loaded_per_root,
        )
        index.discover()
        _index_cache = (key, monotonic(), index) if ttl > 0 else None
        return index


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
