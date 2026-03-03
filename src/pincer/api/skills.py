"""Skills API — FastAPI endpoints for skill list."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from pincer.tools.skills.loader import SkillManifest
from pincer.tools.skills.scanner import SkillScanner

router = APIRouter(prefix="/api/skills", tags=["skills"])

# Project root for bundled skills (src/pincer/api/skills.py -> ../../../..)
_API_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _API_DIR.parent.parent.parent
_BUNDLED_SKILLS = _PROJECT_ROOT / "skills"
_USER_SKILLS = Path.home() / ".pincer" / "skills"
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}


def _discover_skill_dirs() -> list[Path]:
    """Find all skill directories in bundled and user locations."""
    dirs: list[Path] = []
    for base in (_BUNDLED_SKILLS, _USER_SKILLS):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            if (entry / "manifest.json").is_file() and (entry / "skill.py").is_file():
                dirs.append(entry)
    return dirs


@router.get("")
async def list_skills() -> dict[str, list[dict]]:
    """List installed skills with metadata and safety score."""
    scanner = SkillScanner(pass_threshold=50)
    skills: list[dict] = []

    for skill_dir in _discover_skill_dirs():
        manifest_path = skill_dir / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = SkillManifest.from_dict(data, str(skill_dir))
        except Exception:
            continue

        scan_result = scanner.scan_directory(str(skill_dir), manifest=manifest)
        tool_names = [t.get("name", "") for t in manifest.tools if t.get("name")]

        skills.append({
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "safety_score": scan_result.score,
            "status": "active" if scan_result.passed else "error",
            "permissions": manifest.permissions,
            "tools": tool_names,
        })

    return {"skills": skills}
