"""Integration detail API — tool catalog and usage stats per integration."""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


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


def _ms365_categories() -> list[dict[str, Any]]:
    pkg = "pincer.integrations.ms365"
    specs = [
        ("Email", "tools_email", "register_email_tools"),
        ("Calendar", "tools_calendar", "register_calendar_tools"),
        ("OneDrive", "tools_onedrive", "register_onedrive_tools"),
        ("To Do", "tools_todo", "register_todo_tools"),
        ("Teams", "tools_teams", "register_teams_tools"),
        ("Contacts", "tools_contacts", "register_contacts_tools"),
        ("OneNote", "tools_onenote", "register_onenote_tools"),
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
        s = get_settings_relaxed()
        d = s.google_oauth_dir()
        return (d / "google_credentials.json").exists() and (
            (d / "google_workspace_token.json").exists() or (d / "google_token.json").exists()
        )
    except Exception:
        return False


def _ms365_active() -> bool:
    try:
        from pincer.integrations.ms365.config import load_config, resolve_cache_path
        cfg = load_config()
        return bool(cfg.enabled and cfg.client_id and resolve_cache_path(cfg).exists())
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
    "ms365": {
        "name": "Microsoft 365",
        "author": "Microsoft",
        "description": "Outlook · Calendar · OneDrive · To Do · Teams · Contacts · OneNote",
        "prefix": "ms365__",
        "categories_fn": _ms365_categories,
        "active_fn": _ms365_active,
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
