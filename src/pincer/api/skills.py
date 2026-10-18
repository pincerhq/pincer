"""Skills API — FastAPI endpoints for discovered SKILL.md skills."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

if TYPE_CHECKING:
    from pincer.tools.skills.index import SkillEntry, SkillIndex

logger = logging.getLogger(__name__)

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


def _entry_summary(entry: SkillEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "status": "active",
        "source": "file",
        "root": entry.skill.source_root,
        # Directory name on disk — GET /api/skills/{name} matches on this, not
        # the frontmatter `name` above, so clients must link using this field.
        "dir": entry.skill.path.name,
    }


def _skill_entries() -> list[dict[str, Any]]:
    """Discover SKILL.md-based skills via the shared SkillIndex (bundled + user, uniformly)."""
    return [_entry_summary(entry) for entry in _build_index().all_skills()]


def _skill_detail(dir_name: str) -> dict[str, Any] | None:
    """Full detail for a single skill, looked up by its directory name on disk.

    Matched against the actual folder name rather than SkillIndex's internal
    (frontmatter-name-keyed) lookup, so this stays correct even if a skill's
    SKILL.md `name` field drifts from its directory name.
    """
    for entry in _build_index().all_skills():
        if entry.skill.path.name == dir_name:
            return {**_entry_summary(entry), "body": entry.skill.body, "files": entry.list_files()}
    return None


@router.get("")
async def list_skills() -> dict[str, list[dict[str, Any]]]:
    """List discovered SKILL.md skills (bundled + user)."""
    return {"skills": await asyncio.to_thread(_skill_entries)}


async def _enqueue_rebuild_job(generation: str, clear_cache: bool, disk_id: str | None) -> dict[str, Any]:
    """Queue `rebuild_skill_index` for the background task workers. Reported, not
    raised, on failure: the app's own index is already rebuilt at this point."""
    from pincer.tasks import TASK_CHANNEL
    from pincer.tasks import app as task_app

    topic = "rebuild_skill_index"
    try:
        await task_app.send_message_json(
            channel=TASK_CHANNEL,
            payload={"generation": generation, "clear_cache": clear_cache, "disk_id": disk_id},
            headers={"topic": topic},
        )
    except Exception as e:
        logger.exception("Could not queue the skill index rebuild job (generation=%s)", generation)
        return {"queued": False, "topic": topic, "error": f"{type(e).__name__}: {e}"}
    return {"queued": True, "topic": topic}


@router.post("/rebuild")
async def rebuild_skills(
    keep_cache: bool = Query(False, description="Keep cached copies of remote skill files instead of re-fetching"),
) -> dict[str, Any]:
    """Rebuild the skill index — the API counterpart of `pincer skills rebuild`.

    Rebuilds this process's index first (so the agent and dashboard served from it
    see the result immediately), then queues a `rebuild_skill_index` background job
    carrying the new generation for the task workers. The job is consumed by one
    worker; processes sharing this one's data volume also follow the generation
    stamp it writes.
    """
    from pincer.config import get_settings_relaxed
    from pincer.services.skills import get_skill_service

    service = get_skill_service(get_settings_relaxed())
    clear_cache = not keep_cache

    def _rebuild() -> tuple[SkillIndex, str | None]:
        return service.rebuild(clear_cache), service.disk_id

    from pincer.tools.skills.index import SkillRootUnreachable

    try:
        index, disk_id = await asyncio.to_thread(_rebuild)
    except SkillRootUnreachable as e:
        # Storage outage or credentials: nothing was announced, the index is unchanged.
        logger.warning("Skill index rebuild failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Skill index rebuild failed, index unchanged: {e}") from e
    except Exception as e:
        logger.exception("Skill index rebuild failed")
        raise HTTPException(status_code=500, detail=f"Skill index rebuild failed: {e}") from e

    generation = service.generation
    assert generation is not None  # rebuild() always records one
    return {
        "generation": generation,
        "cache_cleared": clear_cache,
        "skills": [_entry_summary(entry) for entry in index.all_skills()],
        "job": await _enqueue_rebuild_job(generation, clear_cache, disk_id),
    }


@router.get("/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    """Get a single skill's full SKILL.md body and file manifest, by directory name."""
    detail = await asyncio.to_thread(_skill_detail, name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {name}")
    return detail
