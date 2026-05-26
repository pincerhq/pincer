"""DEPRECATED! Skills API — FastAPI endpoints for skill list."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from pincer.tools.skills.loader import SkillManifest
from pincer.tools.skills.scanner import SkillScanner
from pincer.api.integrations import list_integrations

router = APIRouter(prefix="/api/skills", tags=["skills"], deprecated=True)


@router.get("")
async def list_skills() -> dict[str, list[dict]]:
    """List installed skills and configured MCP servers."""
    respond = await list_integrations()
    return {"skills": respond.get('integrations', [])}
