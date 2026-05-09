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


def _integration_entries() -> list[dict]:
    """Return Google Workspace, MS365, and Slack as skill-like entries."""
    entries: list[dict] = []

    # ── Google Workspace ─────────────────────────────────────────────────────
    try:
        from pincer.config import get_settings_relaxed

        settings = get_settings_relaxed()
        oauth_dir = settings.google_oauth_dir()
        creds = oauth_dir / "google_credentials.json"
        token = oauth_dir / "google_workspace_token.json"
        legacy = oauth_dir / "google_token.json"
        google_active = creds.exists() and (token.exists() or legacy.exists())
    except Exception:
        google_active = False

    entries.append(
        {
            "name": "Google Workspace",
            "version": "112 tools",
            "description": "Gmail · Calendar · Drive · Docs · Sheets · Slides · Tasks · Contacts · Meet",
            "author": "Google",
            "safety_score": 100,
            "status": "active" if google_active else "disabled",
            "permissions": [],
            "tools": ["gmail", "calendar", "drive", "docs", "sheets", "slides", "tasks", "contacts", "meet"],
            "source": "integration",
            "slug": "google",
        }
    )

    # ── Microsoft 365 ────────────────────────────────────────────────────────
    try:
        from pincer.integrations.ms365.config import load_config, resolve_cache_path

        ms_cfg = load_config()
        ms_token = resolve_cache_path(ms_cfg)
        ms_active = bool(ms_cfg.enabled and ms_cfg.client_id and ms_token.exists())
    except Exception:
        ms_active = False

    entries.append(
        {
            "name": "Microsoft 365",
            "version": "69 tools",
            "description": "Outlook · Calendar · OneDrive · To Do · Teams · Contacts · OneNote",
            "author": "Microsoft",
            "safety_score": 100,
            "status": "active" if ms_active else "disabled",
            "permissions": [],
            "tools": ["email", "calendar", "onedrive", "todo", "teams", "contacts", "onenote"],
            "source": "integration",
            "slug": "ms365",
        }
    )

    # ── Slack ─────────────────────────────────────────────────────────────────
    try:
        from pincer.integrations.slack.auth import load_tokens

        slack_tokens = load_tokens()
        slack_active = bool(slack_tokens.bot_token)
    except Exception:
        slack_active = False

    entries.append(
        {
            "name": "Slack",
            "version": "71 tools",
            "description": "Channels · Messages · Files · Users · Reactions · Reminders",
            "author": "Slack",
            "safety_score": 100,
            "status": "active" if slack_active else "disabled",
            "permissions": [],
            "tools": ["channels", "messages", "files", "users", "reactions", "misc"],
            "source": "integration",
            "slug": "slack",
        }
    )

    return entries


def _mcp_skill_entries() -> list[dict]:
    """Return MCP servers from config as skill-like entries."""
    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        return []

    try:
        mcp_config = load_mcp_config()
    except Exception:
        return []

    if not mcp_config.enabled:
        return []

    entries: list[dict] = []
    for srv in mcp_config.servers:
        if srv.command:
            cmd_desc = f"{srv.command} {' '.join(srv.args)}".strip()
        elif srv.url:
            cmd_desc = srv.url
        else:
            cmd_desc = srv.transport.value

        entries.append(
            {
                "name": srv.name,
                "version": srv.transport.value,
                "description": cmd_desc,
                "author": "",
                "safety_score": 100,
                "status": "active" if srv.enabled else "disabled",
                "permissions": srv.approval_required,
                "tools": [],
                "source": "mcp",
            }
        )
    return entries


@router.get("")
async def list_skills() -> dict[str, list[dict]]:
    """List installed skills and configured MCP servers."""
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

        skills.append(
            {
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "safety_score": scan_result.score,
                "status": "active" if scan_result.passed else "error",
                "permissions": manifest.permissions,
                "tools": tool_names,
                "source": "file",
            }
        )

    skills.extend(_integration_entries())
    skills.extend(_mcp_skill_entries())
    return {"skills": skills}
