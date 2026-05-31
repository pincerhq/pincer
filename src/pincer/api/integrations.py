"""Integration detail API — tool catalog and usage stats per integration."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from pincer.tools.skills.loader import SkillManifest
from pincer.tools.skills.scanner import SkillScanner

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Project root for bundled skills (src/pincer/api/skills.py -> ../../../..)
_API_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _API_DIR.parent.parent.parent
_BUNDLED_SKILLS = _PROJECT_ROOT / "skills"
_USER_SKILLS = Path.home() / ".pincer" / "skills"
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}

# ── List integrations ────────────────────────────────────────────


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


def _integration_entries() -> list[dict[str, Any]]:
    """Return Google Workspace and Slack as skill-like entries."""
    entries: list[dict[str, Any]] = []

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


def _mcp_skill_entries() -> list[dict[str, Any]]:
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

    entries: list[dict[str, Any]] = []
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
async def list_integrations() -> dict[str, list[dict[str, Any]]]:
    """List installed skills and configured MCP servers."""
    scanner = SkillScanner(pass_threshold=50)
    integrations: list[dict[str, Any]] = []

    for skill_dir in _discover_skill_dirs():
        manifest_path = skill_dir / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = SkillManifest.from_dict(data, str(skill_dir))
        except Exception:
            continue

        scan_result = scanner.scan_directory(str(skill_dir), manifest=manifest)
        tool_names = [t.get("name", "") for t in manifest.tools if t.get("name")]

        integrations.append(
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

    integrations.extend(_integration_entries())
    integrations.extend(_mcp_skill_entries())
    return {"integrations": integrations}


# ── Get integration ──────────────────────────────────────────────


class _ToolCollector:
    """Drop-in ToolRegistry that records registrations without executing them."""

    def __init__(self) -> None:
        self.tools: list[dict[str, str]] = []

    def register(self, name: str, description: str = "", **_kwargs: Any) -> None:
        self.tools.append({"name": name, "description": description})


def _load_category(pkg: str, module: str, fn: str) -> list[dict[str, str]]:
    try:
        mod = importlib.import_module(f"{pkg}.{module}")
        collector = _ToolCollector()
        getattr(mod, fn)(collector, None)
        return collector.tools
    except Exception:
        return []


def _google_categories() -> list[dict[str, Any]]:
    pkg = "pincer.integrations.google"
    specs = [
        ("Gmail", "tools_gmail", "register_gmail_tools"),
        ("Calendar", "tools_calendar", "register_calendar_tools"),
        ("Drive", "tools_drive", "register_drive_tools"),
        ("Docs", "tools_docs", "register_docs_tools"),
        ("Sheets", "tools_sheets", "register_sheets_tools"),
        ("Slides", "tools_slides", "register_slides_tools"),
        ("Tasks", "tools_tasks", "register_tasks_tools"),
        ("Contacts", "tools_contacts", "register_contacts_tools"),
        ("Meet", "tools_meet", "register_meet_tools"),
    ]
    return [{"name": n, "tools": t} for n, m, f in specs if (t := _load_category(pkg, m, f))]


def _slack_categories() -> list[dict[str, Any]]:
    pkg = "pincer.integrations.slack"
    specs = [
        ("Channels", "tools_channels", "register_channel_tools"),
        ("Messages", "tools_messages", "register_message_tools"),
        ("Files", "tools_files", "register_file_tools"),
        ("Users", "tools_users", "register_user_tools"),
        ("Reactions", "tools_reactions", "register_reaction_tools"),
        ("Misc", "tools_misc", "register_misc_tools"),
    ]
    return [{"name": n, "tools": t} for n, m, f in specs if (t := _load_category(pkg, m, f))]


def _google_active() -> bool:
    try:
        from pincer.config import get_settings_relaxed

        settings = get_settings_relaxed()
        d = settings.google_oauth_dir()
        return (d / "google_credentials.json").exists() and (
            (d / "google_workspace_token.json").exists() or (d / "google_token.json").exists()
        )
    except Exception:
        return False


def _slack_active() -> bool:
    try:
        from pincer.integrations.slack.auth import load_tokens

        return bool(load_tokens().bot_token)
    except Exception:
        return False


_CATALOG: dict[str, dict[str, Any]] = {
    "google": {
        "name": "Google Workspace",
        "author": "Google",
        "description": "Gmail · Calendar · Drive · Docs · Sheets · Slides · Tasks · Contacts · Meet",
        "prefix": "google__",
        "categories_fn": _google_categories,
        "active_fn": _google_active,
    },
    "slack": {
        "name": "Slack",
        "author": "Slack",
        "description": "Channels · Messages · Files · Users · Reactions · Reminders",
        "prefix": "slack__",
        "categories_fn": _slack_categories,
        "active_fn": _slack_active,
    },
}


async def _tool_usage(prefix: str) -> dict[str, int]:
    try:
        from pincer.security.audit import get_audit_logger

        logger = await get_audit_logger()
        stats = await logger.get_stats()
        by_tool: dict[str, int] = stats.get("by_tool", {})
        return {k: v for k, v in by_tool.items() if k and k.startswith(prefix)}
    except Exception:
        return {}


@router.get("/{slug}")
async def get_integration(slug: str) -> dict[str, Any]:
    if slug not in _CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown integration: {slug}")

    meta = _CATALOG[slug]
    categories = meta["categories_fn"]()
    usage = await _tool_usage(meta["prefix"])

    return {
        "slug": slug,
        "name": meta["name"],
        "author": meta["author"],
        "description": meta["description"],
        "status": "active" if meta["active_fn"]() else "disabled",
        "tool_count": sum(len(c["tools"]) for c in categories),
        "categories": categories,
        "usage": {
            "total_calls": sum(usage.values()),
            "by_tool": usage,
        },
    }
