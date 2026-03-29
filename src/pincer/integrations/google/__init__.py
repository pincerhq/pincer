"""
Google Workspace integration for Pincer.

Provides 112 tools across Gmail, Calendar, Drive, Docs, Sheets, Slides,
Tasks, Contacts, and Meet using Google's official Python client libraries.

Quick start::

    from pincer.integrations.google import get_google_factory, register_all_tools
    from pincer.tools.registry import ToolRegistry

    factory = get_google_factory()   # None if not configured
    if factory:
        registry = ToolRegistry()
        register_all_tools(registry, factory)

Setup::

    pincer setup-google   # one-time OAuth consent flow
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry


def get_google_factory() -> GoogleServiceFactory | None:
    """Return a GoogleServiceFactory if credentials are configured, else None.

    Performs an eager scope check so a stale token is surfaced at startup
    (with a clear warning) rather than mid-conversation when a tool is invoked.
    """
    import logging

    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.auth import GoogleAuth
    from pincer.integrations.google.service_factory import GoogleServiceFactory

    _log = logging.getLogger(__name__)

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_workspace_token.json"

    if not credentials_path.exists() or not token_path.exists():
        # Fall back to legacy calendar token if it covers workspace scopes
        legacy_token = oauth_dir / "google_token.json"
        if credentials_path.exists() and legacy_token.exists():
            token_path = legacy_token
        else:
            return None

    auth = GoogleAuth(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
    )

    # Eager scope check — catch stale tokens before any tool call is made.
    missing = auth.missing_scopes()
    if missing:
        _log.warning(
            "Google Workspace token is missing %d required scope(s) — "
            "Google tools will be disabled until you re-authenticate.\n"
            "  Missing: %s\n"
            "  Fix: delete %s  then run  pincer setup-google",
            len(missing),
            missing,
            token_path,
        )
        return None

    return GoogleServiceFactory(auth)


def register_all_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 112 Google Workspace tools in *registry*. Returns count."""
    from pincer.integrations.google.tools_calendar import register_calendar_tools
    from pincer.integrations.google.tools_contacts import register_contacts_tools
    from pincer.integrations.google.tools_docs import register_docs_tools
    from pincer.integrations.google.tools_drive import register_drive_tools
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.integrations.google.tools_meet import register_meet_tools
    from pincer.integrations.google.tools_sheets import register_sheets_tools
    from pincer.integrations.google.tools_slides import register_slides_tools
    from pincer.integrations.google.tools_tasks import register_tasks_tools

    count = 0
    count += register_gmail_tools(registry, factory)
    count += register_calendar_tools(registry, factory)
    count += register_drive_tools(registry, factory)
    count += register_docs_tools(registry, factory)
    count += register_sheets_tools(registry, factory)
    count += register_slides_tools(registry, factory)
    count += register_tasks_tools(registry, factory)
    count += register_contacts_tools(registry, factory)
    count += register_meet_tools(registry, factory)
    return count


__all__ = ["get_google_factory", "register_all_tools"]
