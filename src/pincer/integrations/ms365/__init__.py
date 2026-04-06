"""
Microsoft 365 integration for Pincer.

Provides 69 tools across Outlook email, Calendar, OneDrive, To Do,
Teams, Contacts, and OneNote using Microsoft Graph REST API.

Quick start::

    from pincer.integrations.ms365 import get_ms365_client, register_all_tools
    from pincer.tools.registry import ToolRegistry

    client = get_ms365_client()   # None if not configured
    if client:
        registry = ToolRegistry()
        register_all_tools(registry, client)

Setup::

    pincer setup-ms365   # one-time device code auth flow
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.integrations.ms365.graph_client import GraphClient
    from pincer.tools.registry import ToolRegistry


def get_ms365_client() -> GraphClient | None:
    """Return a GraphClient if MS365 is configured, else None.

    Checks for client_id in config/environment and a cached token.
    """
    import logging

    from pincer.integrations.ms365.auth import MS365Auth
    from pincer.integrations.ms365.config import load_config, resolve_cache_path
    from pincer.integrations.ms365.graph_client import GraphClient

    _log = logging.getLogger(__name__)

    cfg = load_config()
    if not cfg.enabled:
        return None

    if not cfg.client_id:
        return None

    cache_path = resolve_cache_path(cfg)
    auth = MS365Auth(
        client_id=cfg.client_id,
        tenant_id=cfg.tenant_id,
        cache_path=str(cache_path),
        services=cfg.services,
    )

    if not auth.has_cached_token():
        _log.warning(
            "Microsoft 365 token not found — tools will be disabled. Run 'pincer setup-ms365' to authenticate."
        )
        return None

    return GraphClient(auth)


def register_all_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 69 Microsoft 365 tools in *registry*. Returns count."""
    from pincer.integrations.ms365.tools_calendar import register_calendar_tools
    from pincer.integrations.ms365.tools_contacts import register_contacts_tools
    from pincer.integrations.ms365.tools_email import register_email_tools
    from pincer.integrations.ms365.tools_onedrive import register_onedrive_tools
    from pincer.integrations.ms365.tools_onenote import register_onenote_tools
    from pincer.integrations.ms365.tools_teams import register_teams_tools
    from pincer.integrations.ms365.tools_todo import register_todo_tools

    count = 0
    count += register_email_tools(registry, client)
    count += register_calendar_tools(registry, client)
    count += register_onedrive_tools(registry, client)
    count += register_todo_tools(registry, client)
    count += register_teams_tools(registry, client)
    count += register_contacts_tools(registry, client)
    count += register_onenote_tools(registry, client)
    return count


__all__ = ["get_ms365_client", "register_all_tools"]
