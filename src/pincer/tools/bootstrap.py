"""Shared tool-registration helper.

Builds a ToolRegistry containing all *channel-independent* tools the agent
can use: web search, file IO, Python/shell exec, browser, email, calendar
builtins, and the full Google Workspace / Microsoft 365 / Slack
integrations when configured.

Used by both `cli._run_agent` (for terminal/messaging-channel agents) and
`api._deps.build_agent_from_settings` (for the web chat agent) so the two
surfaces expose the same tool suite.

Channel-specific tools (`send_file`, `send_image`), Skills, and the MCP
client manager are NOT registered here — they need state owned by the
caller and stay in `cli._run_agent`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_default_tools(tools: ToolRegistry, settings: Settings) -> dict[str, int]:
    """Register the default tool suite in *tools*.

    Returns a small report dict so the caller can log what came on/off
    (e.g. ``{"google": 112, "ms365": 69, "slack": 71, "builtins": 12}``).
    """
    report: dict[str, int] = {"builtins": 0}

    _register_builtins(tools, settings, report)
    _register_google_workspace(tools, report)
    _register_ms365(tools, report)
    _register_slack(tools, report)
    _register_image_gen(tools, settings, report)

    return report


def _register_builtins(tools: ToolRegistry, settings: Settings, report: dict[str, int]) -> None:
    from pincer.tools.builtin.files import file_list, file_read, file_write
    from pincer.tools.builtin.python_exec import python_exec
    from pincer.tools.builtin.web_search import web_search

    tools.register(
        name="web_search",
        description=(
            "Search the web for current information. Use for any question about recent "
            "events, facts you're not sure about, or anything that would benefit from "
            "up-to-date information."
        ),
        handler=web_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "num_results": {
                    "type": "integer",
                    "description": "Number of results (1-10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    )
    report["builtins"] += 1

    if settings.shell_enabled:
        from pincer.tools.builtin.shell import shell_exec

        tools.register(
            name="shell_exec",
            description=(
                "Execute a shell command on the user's machine. Use for system tasks, "
                "running scripts, checking system info, git operations, etc. "
                "Always explain what the command does before running it."
            ),
            handler=shell_exec,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory",
                        "default": "~",
                    },
                },
                "required": ["command"],
            },
            require_approval=settings.shell_require_approval,
        )
        report["builtins"] += 1

    tools.register(
        name="file_read",
        description="Read a file's content from the workspace.",
        handler=file_read,
    )
    tools.register(
        name="file_write",
        description="Write content to a file in the workspace.",
        handler=file_write,
    )
    tools.register(
        name="file_list",
        description="List files in a workspace directory.",
        handler=file_list,
    )
    report["builtins"] += 3

    try:
        from pincer.tools.builtin.browser import browse, screenshot

        tools.register(
            name="browse",
            description=(
                "Navigate to a URL and return the page's readable text content. "
                "Use for reading web pages, articles, documentation, etc."
            ),
            handler=browse,
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to navigate to",
                    },
                },
                "required": ["url"],
            },
        )
        tools.register(
            name="screenshot",
            description="Take a screenshot of a web page. Use when the user wants to see what a page looks like.",
            handler=screenshot,
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to screenshot",
                    },
                },
                "required": ["url"],
            },
        )
        report["builtins"] += 2
    except ImportError:
        logger.debug("Playwright not installed, browser tools disabled")

    tools.register(
        name="python_exec",
        description=(
            "Execute Python code in an isolated subprocess and return the output. "
            "Use for calculations, data analysis, generating charts, or running scripts. "
            "Common libraries available: pandas, numpy, matplotlib, fpdf2. "
            "Generated files are saved to ~/.pincer/workspace/exec_output/ — "
            "use send_file to deliver them to the user."
        ),
        handler=python_exec,
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 30, max 120)",
                    "default": 30,
                },
            },
            "required": ["code"],
        },
    )
    report["builtins"] += 1

    if settings.email_imap_host and settings.email_username:
        _register_email_tools(tools, report)

    _register_calendar_builtins(tools, report)


def _register_email_tools(tools: ToolRegistry, report: dict[str, int]) -> None:
    from pincer.tools.builtin.email_tool import (
        email_check,
        email_empty_folder,
        email_list_folders,
        email_mark,
        email_move,
        email_read,
        email_search,
        email_send,
        email_trash,
    )

    tools.register(
        name="email_check",
        description=(
            "Check emails in a folder. By default shows unread (UNSEEN) emails. "
            "Set status='ALL' to list all emails regardless of read status — "
            "use this when checking Spam, Trash, or counting total emails in a folder. "
            "Returns UID, sender, subject, and date for each."
        ),
        handler=email_check,
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "folder": {"type": "string", "default": "INBOX"},
                "status": {"type": "string", "default": "UNSEEN"},
            },
            "required": [],
        },
    )
    tools.register(
        name="email_send",
        description=(
            "Send an email. Requires recipient, subject, body. "
            "Use when the user asks to send, write, or compose an email."
        ),
        handler=email_send,
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string", "default": ""},
            },
            "required": ["to", "subject", "body"],
        },
        require_approval=True,
    )
    tools.register(
        name="email_search",
        description="Search emails by keyword, sender, or date range. Returns UIDs for each match.",
        handler=email_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sender": {"type": "string", "default": ""},
                "days_back": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 10},
                "folder": {"type": "string", "default": "INBOX"},
            },
            "required": ["query"],
        },
    )
    tools.register(
        name="email_read",
        description="Read the full content of an email by its UID.",
        handler=email_read,
        parameters={
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "folder": {"type": "string", "default": "INBOX"},
                "max_chars": {"type": "integer", "default": 4000},
            },
            "required": ["uid"],
        },
    )
    tools.register(
        name="email_list_folders",
        description="List all available IMAP/email folders.",
        handler=email_list_folders,
        parameters={"type": "object", "properties": {}, "required": []},
    )
    tools.register(
        name="email_mark",
        description="Mark one or more emails as read, unread, flagged, or unflagged.",
        handler=email_mark,
        parameters={
            "type": "object",
            "properties": {
                "uids": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["read", "unread", "flag", "unflag"],
                },
                "folder": {"type": "string", "default": "INBOX"},
            },
            "required": ["uids", "action"],
        },
    )
    tools.register(
        name="email_move",
        description="Move one or more emails to a different folder.",
        handler=email_move,
        parameters={
            "type": "object",
            "properties": {
                "uids": {"type": "string"},
                "destination": {"type": "string"},
                "folder": {"type": "string", "default": "INBOX"},
            },
            "required": ["uids", "destination"],
        },
        require_approval=True,
    )
    tools.register(
        name="email_trash",
        description="Delete specific emails by moving them to Trash.",
        handler=email_trash,
        parameters={
            "type": "object",
            "properties": {
                "uids": {"type": "string"},
                "folder": {"type": "string", "default": "INBOX"},
            },
            "required": ["uids"],
        },
        require_approval=True,
    )
    tools.register(
        name="email_empty_folder",
        description="Delete all emails in a folder such as Spam or Trash.",
        handler=email_empty_folder,
        parameters={
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "required": ["folder"],
        },
        require_approval=True,
    )
    report["builtins"] += 9


def _register_calendar_builtins(tools: ToolRegistry, report: dict[str, int]) -> None:
    try:
        from pincer.tools.builtin.calendar_tool import (
            calendar_create,
            calendar_today,
            calendar_week,
        )
    except ImportError:
        logger.debug("Google Calendar dependencies not installed, calendar builtins disabled")
        return

    tools.register(
        name="calendar_today",
        description=(
            "Get today's calendar events. Use when user asks about their schedule, meetings, or agenda today."
        ),
        handler=calendar_today,
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": [],
        },
    )
    tools.register(
        name="calendar_week",
        description="Get this week's calendar events. Use when user asks about their week or upcoming schedule.",
        handler=calendar_week,
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": [],
        },
    )
    tools.register(
        name="calendar_create",
        description=(
            "Create a new Google Calendar event. Use when user asks to "
            "schedule, add, or book a meeting or event. "
            "On success, the tool returns the event's direct URL (htmlLink). "
            "You MUST include this URL in your final response so the user can open the event."
        ),
        handler=calendar_create,
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {"type": "string"},
                "duration_minutes": {"type": "integer", "default": 60},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["title", "start_time"],
        },
        require_approval=True,
    )
    report["builtins"] += 3


def _register_google_workspace(tools: ToolRegistry, report: dict[str, int]) -> None:
    try:
        from pincer.integrations.google import get_google_factory, register_all_tools

        factory = get_google_factory()
        if factory is None:
            return
        report["google"] = register_all_tools(tools, factory)
    except RuntimeError as e:
        logger.warning("Google Workspace tools disabled: %s", e)
    except Exception:
        logger.debug("Google Workspace tools not loaded", exc_info=True)


def _register_ms365(tools: ToolRegistry, report: dict[str, int]) -> None:
    try:
        from pincer.integrations.ms365 import get_ms365_client
        from pincer.integrations.ms365 import register_all_tools as register_ms365_tools

        client = get_ms365_client()
        if client is None:
            return
        report["ms365"] = register_ms365_tools(tools, client)
    except RuntimeError as e:
        logger.warning("Microsoft 365 tools disabled: %s", e)
    except Exception:
        logger.debug("Microsoft 365 tools not loaded", exc_info=True)


def _register_slack(tools: ToolRegistry, report: dict[str, int]) -> None:
    try:
        from pincer.integrations.slack import get_slack_client
        from pincer.integrations.slack import register_all_tools as register_slack_tools

        client = get_slack_client()
        if client is None:
            return
        report["slack"] = register_slack_tools(tools, client)
    except RuntimeError as e:
        logger.warning("Slack tools disabled: %s", e)
    except Exception:
        logger.debug("Slack tools not loaded", exc_info=True)


def _register_image_gen(tools: ToolRegistry, settings: Settings, report: dict[str, int]) -> None:
    fal_key = settings.fal_key.get_secret_value()
    gemini_key = settings.gemini_api_key.get_secret_value()
    if not fal_key and not gemini_key:
        return
    try:
        from pincer.image.router import build_router_from_settings
        from pincer.tools.builtin.image_gen import make_generate_image_handler

        router = build_router_from_settings()
        tools.register(
            name="generate_image",
            description=(
                "Generate image(s) from a text prompt and send them directly to the user. "
                "Use for creating original images, illustrations, or artwork."
            ),
            handler=make_generate_image_handler(router),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "caption": {"type": "string", "default": ""},
                    "aspect_ratio": {"type": "string", "default": "1:1"},
                    "num_images": {"type": "integer", "default": 1},
                },
                "required": ["prompt"],
            },
        )
        report["image_gen"] = 1
    except Exception:
        logger.debug("Image generation tool not loaded", exc_info=True)
