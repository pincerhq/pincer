"""
Google Workspace integration for Pincer.

Provides up to 112 tools across Gmail, Calendar, Drive, Docs, Sheets, Slides,
Tasks, Contacts, and Meet using Google's official Python client libraries.

Quick start::

    from pincer.integrations.google import get_google_factory, register_all_tools
    from pincer.tools.registry import ToolRegistry

    factory = get_google_factory()   # None if not configured / no scopes granted
    if factory:
        registry = ToolRegistry()
        register_all_tools(registry, factory)

Setup::

    pincer google setup   # one-time OAuth consent flow
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.google.auth import GoogleAuth
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry


def _resolve_auth() -> tuple[GoogleAuth, list[str], list[str]] | None:
    """Build a GoogleAuth limited to the services the cached token covers.

    Returns ``(auth, skipped_services, missing_scopes)``, or None when the
    integration is disabled in config or no token is configured (neither
    ``PINCER_GOOGLE_TOKEN_JSON`` nor the credential/token files).
    ``auth.services`` may be empty when the token covers none of the
    configured services.
    """
    from pincer.config import get_settings_relaxed
    from pincer.integrations.google import config
    from pincer.integrations.google.auth import SERVICE_SCOPES, GoogleAuth
    from pincer.integrations.google.token_source import locate_token

    cfg = config.load_config()
    if not cfg.enabled:
        return None
    unknown = [s for s in cfg.services if s not in SERVICE_SCOPES]
    if unknown:
        logging.getLogger(__name__).warning("Ignoring unknown [integrations.google] services: %s", unknown)
    configured = [s for s in cfg.services if s in SERVICE_SCOPES]

    location = locate_token(get_settings_relaxed())
    if location is None:
        return None

    probe = GoogleAuth(**location, services=configured)
    granted = probe.granted_services()
    services = [s for s in configured if s in granted]
    skipped = [s for s in configured if s not in granted]
    auth = GoogleAuth(**location, services=services)
    return auth, skipped, probe.missing_scopes()


def google_status() -> dict[str, Any]:
    """Summarise the integration state for the UI/API.

    ``status`` is ``active`` (every configured service enabled), ``partial``
    (the token lacks scopes for some services) or ``disabled``.
    """
    resolved = _resolve_auth()
    if resolved is None:
        return {"status": "disabled", "enabled_services": [], "missing_scopes": [], "source": None}
    auth, skipped, missing = resolved
    if not auth.services:
        status = "disabled"
    elif skipped:
        status = "partial"
    else:
        status = "active"
    return {"status": status, "enabled_services": auth.services, "missing_scopes": missing, "source": auth.source}


def get_google_factory() -> GoogleServiceFactory | None:
    """Return a GoogleServiceFactory for the services the token covers, else None.

    Performs an eager scope check so a stale token is surfaced at startup
    (with a clear warning) rather than mid-conversation when a tool is invoked.
    Services whose scopes are missing are skipped; the rest stay enabled.
    """
    from pincer.integrations.google.service_factory import GoogleServiceFactory

    _log = logging.getLogger(__name__)

    resolved = _resolve_auth()
    if resolved is None:
        return None
    auth, skipped, missing = resolved

    if skipped:
        _log.warning(
            "Google Workspace token is missing %d required scope(s) — "
            "%s tools disabled until you re-authenticate.\n"
            "  Missing: %s\n"
            "  Fix: run  %s",
            len(missing),
            ", ".join(skipped),
            missing,
            "pincer google setup --print-env  and update PINCER_GOOGLE_TOKEN_JSON"
            if auth.source == "env"
            else "pincer google setup",
        )
    if not auth.services:
        return None

    _log.info(
        "Google Workspace token loaded from %s",
        "PINCER_GOOGLE_TOKEN_JSON" if auth.source == "env" else auth.token_path,
    )
    return GoogleServiceFactory(auth)


def register_all_tools(
    registry: ToolRegistry,
    factory: GoogleServiceFactory,
    services: list[str] | None = None,
) -> int:
    """Register Google Workspace tools in *registry*. Returns count.

    Only *services* are registered (default: ``factory.auth.services``);
    with every service enabled that is all 112 tools.
    """
    from pincer.integrations.google.tools_calendar import register_calendar_tools
    from pincer.integrations.google.tools_contacts import register_contacts_tools
    from pincer.integrations.google.tools_docs import register_docs_tools
    from pincer.integrations.google.tools_drive import register_drive_tools
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.integrations.google.tools_meet import register_meet_tools
    from pincer.integrations.google.tools_sheets import register_sheets_tools
    from pincer.integrations.google.tools_slides import register_slides_tools
    from pincer.integrations.google.tools_tasks import register_tasks_tools

    registrars = {
        "gmail": register_gmail_tools,
        "calendar": register_calendar_tools,
        "drive": register_drive_tools,
        "docs": register_docs_tools,
        "sheets": register_sheets_tools,
        "slides": register_slides_tools,
        "tasks": register_tasks_tools,
        "contacts": register_contacts_tools,
        "meet": register_meet_tools,  # covers both the meet and meet_beta APIs
    }

    if services is None:
        services = factory.auth.services
    count = 0
    for name, register in registrars.items():
        if name in services:
            count += register(registry, factory)
    return count


__all__ = ["get_google_factory", "google_status", "register_all_tools"]
