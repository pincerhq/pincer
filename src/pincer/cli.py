"""
Pincer CLI — the main entry point.

Usage:
    pincer run          Start the agent
    pincer config       Show current configuration
    pincer cost         Show today's spend
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from datetime import UTC
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    from pincer.channels.base import BaseChannel, IncomingMessage
    from pincer.config import Settings

logger = logging.getLogger(__name__)
app = typer.Typer(
    name="pincer",
    help="Pincer — Your personal AI agent",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, markup=True)],
    )


def _find_env_file() -> str:
    """Return the path to the .env file (project root preferred, else home dir)."""
    from pathlib import Path

    candidates = [Path(".env"), Path("../.env"), Path.home() / ".pincer" / ".env"]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return str(Path(".env").resolve())


def _upsert_env(env_path: str, key: str, value: str) -> None:
    """Set or update a KEY=VALUE pair in an .env file."""
    from pathlib import Path

    path = Path(env_path)
    lines: list[str] = []
    found = False
    if path.exists():
        lines = path.read_text().splitlines(keepends=True)
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    path.write_text("".join(new_lines))


def _port_in_use(host: str, port: int) -> bool:
    """Return True if something is already listening on the given host:port."""
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((check_host, port)) == 0


@app.command()
def run() -> None:
    """Start the Pincer agent."""
    from pincer.config import get_settings

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    _setup_logging(settings.log_level.value)
    console.print(f"[bold green]{settings.agent_name} starting...[/bold green]")
    console.print(f"   Provider: {settings.default_provider.value}")
    console.print(f"   Model: {settings.default_model}")
    console.print(f"   Budget: ${settings.daily_budget_usd:.2f}/day")
    console.print(f"   Data: {settings.data_dir}")
    console.print()

    asyncio.run(_run_agent(settings))


async def _run_agent(settings: Settings) -> None:
    from pincer.core.agent import Agent
    from pincer.core.session import SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.memory.store import MemoryStore
    from pincer.memory.summarizer import Summarizer
    from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger
    from pincer.security.rate_limiter import get_rate_limiter
    from pincer.tools.builtin.files import file_list, file_read, file_write
    from pincer.tools.builtin.shell import shell_exec
    from pincer.tools.builtin.web_search import web_search
    from pincer.tools.registry import ToolRegistry

    # Initialize components
    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()

    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    # Sprint 5: Security components
    audit_logger = None
    if not settings.audit_disabled:
        audit_db = settings.data_dir / "audit.db"
        audit_logger = await get_audit_logger(audit_db)
        console.print("[green]Audit logging enabled[/green]")

    rate_limiter = get_rate_limiter(
        messages_per_minute=settings.rate_messages_per_min,
        tool_calls_per_minute=settings.rate_tools_per_min,
        max_concurrent_llm=settings.max_concurrent_llm,
        max_daily_spend_usd=settings.daily_budget_usd,
    )

    # Initialize memory system
    memory_store: MemoryStore | None = None
    summarizer: Summarizer | None = None

    # Create LLM provider
    if settings.default_provider.value == "anthropic":
        from pincer.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(settings)
    elif settings.default_provider.value == "grok":
        from pincer.llm.grok_provider import GrokProvider

        llm = GrokProvider(settings)
    else:
        from pincer.llm.openai_provider import OpenAIProvider

        llm = OpenAIProvider(settings)

    if settings.memory_enabled:
        memory_store = MemoryStore(settings.db_path)
        await memory_store.initialize()
        summarizer = Summarizer(
            llm=llm,
            memory_store=memory_store,
            session_manager=session_mgr,
            summary_model=settings.summary_model,
            threshold=settings.summary_threshold,
        )
        console.print("[green]Memory system enabled[/green]")

    # Register tools
    tools = ToolRegistry()
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
    if settings.shell_enabled:
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

    # Browser tools (optional — requires playwright)
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
            description=("Take a screenshot of a web page. Use when the user wants to see what a page looks like."),
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
    except ImportError:
        logging.getLogger(__name__).debug("Playwright not installed, browser tools disabled")

    # Python execution tool
    from pincer.tools.builtin.python_exec import python_exec

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

    # Email tools (Sprint 3)
    if settings.email_imap_host and settings.email_username:
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
                    "limit": {
                        "type": "integer",
                        "description": "Max emails to return (default: 10)",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter: UNSEEN (default, unread only), ALL (all emails), SEEN (read only)",
                        "default": "UNSEEN",
                    },
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
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body (plain text)"},
                    "cc": {
                        "type": "string",
                        "description": "CC recipients (comma-separated)",
                        "default": "",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        )
        tools.register(
            name="email_search",
            description=(
                "Search emails by keyword, sender, or date range. Returns UIDs for each match. "
                "Use when the user asks to find a specific email or topic. "
                "Can search in any folder, not just INBOX."
            ),
            handler=email_search,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "sender": {
                        "type": "string",
                        "description": "Filter by sender email",
                        "default": "",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Days to search back (default: 30)",
                        "default": 30,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder to search in (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["query"],
            },
        )
        tools.register(
            name="email_read",
            description=(
                "Read the full content of an email by its UID. "
                "Use after email_check or email_search to read a specific email's body. "
                "Returns headers and the plain-text body."
            ),
            handler=email_read,
            parameters={
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID (from email_check or email_search)"},
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder the email is in (default: INBOX)",
                        "default": "INBOX",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max body characters to return (default: 4000)",
                        "default": 4000,
                    },
                },
                "required": ["uid"],
            },
        )
        tools.register(
            name="email_list_folders",
            description=(
                "List all available IMAP/email folders. "
                "Use to discover folder names before moving emails or emptying spam/trash."
            ),
            handler=email_list_folders,
            parameters={"type": "object", "properties": {}, "required": []},
        )
        tools.register(
            name="email_mark",
            description=(
                "Mark one or more emails as read, unread, flagged, or unflagged. "
                "Use for email triage — e.g. marking emails as read after reviewing them."
            ),
            handler=email_mark,
            parameters={
                "type": "object",
                "properties": {
                    "uids": {
                        "type": "string",
                        "description": "Comma-separated email UIDs to mark",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["read", "unread", "flag", "unflag"],
                        "description": "Action to perform: read, unread, flag, or unflag",
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["uids", "action"],
            },
        )
        tools.register(
            name="email_move",
            description=(
                "Move one or more emails to a different folder. "
                "Use for triage — e.g. archiving emails or moving to a label/folder. "
                "Use email_list_folders first to discover available folder names."
            ),
            handler=email_move,
            parameters={
                "type": "object",
                "properties": {
                    "uids": {
                        "type": "string",
                        "description": "Comma-separated email UIDs to move",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Target folder name (use email_list_folders to discover names)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Source IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["uids", "destination"],
            },
            require_approval=True,
        )
        tools.register(
            name="email_trash",
            description=(
                "Delete specific emails by moving them to Trash. "
                "Pass one or more comma-separated UIDs. "
                "Auto-discovers the correct trash folder name for the email provider."
            ),
            handler=email_trash,
            parameters={
                "type": "object",
                "properties": {
                    "uids": {
                        "type": "string",
                        "description": "Comma-separated email UIDs to trash",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Source IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["uids"],
            },
            require_approval=True,
        )
        tools.register(
            name="email_empty_folder",
            description=(
                "Delete all emails in a folder such as Spam or Trash. "
                "Common folder names: '[Gmail]/Spam', '[Gmail]/Trash'. "
                "Cannot empty INBOX. Use email_list_folders if unsure about folder names."
            ),
            handler=email_empty_folder,
            parameters={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder to empty (e.g. '[Gmail]/Spam', '[Gmail]/Trash')",
                    },
                },
                "required": ["folder"],
            },
            require_approval=True,
        )
        console.print("[green]Email tools enabled[/green]")

    # Google Calendar tools (Sprint 3)
    try:
        from pincer.tools.builtin.calendar_tool import (
            calendar_create,
            calendar_today,
            calendar_week,
        )

        tools.register(
            name="calendar_today",
            description=(
                "Get today's calendar events. Use when user asks about their schedule, meetings, or agenda today."
            ),
            handler=calendar_today,
            parameters={
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
                },
                "required": [],
            },
        )
        tools.register(
            name="calendar_week",
            description=("Get this week's calendar events. Use when user asks about their week or upcoming schedule."),
            handler=calendar_week,
            parameters={
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
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
                "You MUST include this URL in your final response so the user can open the event. "
                "On failure, the tool returns an error message. Pass that exact message to the user; "
                "do not claim success."
            ),
            handler=calendar_create,
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "start_time": {
                        "type": "string",
                        "description": "Start time in ISO 8601, e.g. '2026-02-22T14:00:00+01:00'",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Duration in minutes (default: 60)",
                        "default": 60,
                    },
                    "description": {
                        "type": "string",
                        "description": "Event description",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location",
                        "default": "",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
                },
                "required": ["title", "start_time"],
            },
        )
        console.print("[green]Calendar tools enabled[/green]")
    except ImportError:
        logging.getLogger(__name__).debug("Google Calendar dependencies not installed, calendar tools disabled")

    # Google Workspace tools (full 85-tool suite)
    try:
        from pincer.integrations.google import get_google_factory, register_all_tools

        gws_factory = get_google_factory()
        if gws_factory is not None:
            gws_count = register_all_tools(tools, gws_factory)
            console.print(f"[green]Google Workspace tools enabled ({gws_count} tools)[/green]")
    except RuntimeError as _gws_exc:
        console.print(f"[yellow]Google Workspace tools disabled:[/yellow] {_gws_exc}")
        console.print("[yellow]  Run [bold]pincer setup-google[/bold] to re-authenticate.[/yellow]")
    except Exception as _gws_exc:
        logging.getLogger(__name__).debug("Google Workspace tools not loaded: %s", _gws_exc)

    # Microsoft 365 tools (69 tools — email, calendar, OneDrive, To Do, Teams, Contacts, OneNote)
    try:
        from pincer.integrations.ms365 import get_ms365_client
        from pincer.integrations.ms365 import register_all_tools as register_ms365_tools

        ms365_client = get_ms365_client()
        if ms365_client is not None:
            ms365_count = register_ms365_tools(tools, ms365_client)
            console.print(f"[green]Microsoft 365 tools enabled ({ms365_count} tools)[/green]")
    except RuntimeError as _ms365_exc:
        console.print(f"[yellow]Microsoft 365 tools disabled:[/yellow] {_ms365_exc}")
        console.print("[yellow]  Run [bold]pincer setup-ms365[/bold] to authenticate.[/yellow]")
    except Exception as _ms365_exc:
        logging.getLogger(__name__).debug("Microsoft 365 tools not loaded: %s", _ms365_exc)

    # Slack tools (71-tool suite)
    try:
        from pincer.integrations.slack import get_slack_client
        from pincer.integrations.slack import register_all_tools as _slack_register

        slack_client = get_slack_client()
        if slack_client is not None:
            slack_count = _slack_register(tools, slack_client)
            console.print(f"[green]Slack tools enabled ({slack_count} tools)[/green]")
    except RuntimeError as _slack_exc:
        console.print(f"[yellow]Slack tools disabled:[/yellow] {_slack_exc}")
        console.print("[yellow]  Run [bold]pincer setup-slack[/bold] to configure.[/yellow]")
    except Exception as _slack_exc:
        logging.getLogger(__name__).debug("Slack tools not loaded: %s", _slack_exc)

    # send_file tool — channels dict is populated after channel startup below
    channel_map: dict[str, BaseChannel] = {}

    async def send_file(path: str, caption: str = "", context: dict | None = None) -> str:
        """Send a file to the user via their messaging channel.

        path: Absolute path to the file to send
        caption: Optional caption/description for the file
        """
        from pathlib import Path as _P

        file_path = _P(path)
        if not file_path.is_file():
            return f"Error: File not found: {path}"

        ctx = context or {}
        user_id = ctx.get("user_id", "")
        ch_name = ctx.get("channel", "")
        channel = channel_map.get(ch_name)
        if not channel or not user_id:
            return f"Error: No active channel to send file (channel={ch_name})"

        await channel.send_file(user_id, str(file_path), caption)
        return f"File sent: {file_path.name}"

    tools.register(
        name="send_file",
        description=(
            "Send a file to the user as a document attachment (PDF, image, CSV, etc.). "
            "Use after python_exec generates a file, or to deliver any workspace file."
        ),
        handler=send_file,
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to send",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the file",
                    "default": "",
                },
            },
            "required": ["path"],
        },
    )

    async def send_image(url: str, caption: str = "", context: dict | None = None) -> str:
        """Send an image or GIF to the user from a URL.

        url: Direct URL to the image or GIF
        caption: Optional caption/description
        """
        ctx = context or {}
        user_id = ctx.get("user_id", "")
        ch_name = ctx.get("channel", "")
        channel = channel_map.get(ch_name)
        if not channel or not user_id:
            return f"Error: No active channel to send image (channel={ch_name})"

        lower = url.lower()
        is_gif = lower.endswith(".gif") or "giphy.com" in lower or "/gif" in lower or "tenor.com" in lower
        try:
            if is_gif:
                await channel.send_animation(user_id, url, caption)
            else:
                await channel.send_photo(user_id, url, caption)
            return "Image sent to user."
        except Exception as e:
            logging.getLogger(__name__).warning("send_image failed for %s: %s", url, e)
            return (
                f"Error: Failed to send image from {url} ({e}). "
                "The URL may be broken or hotlink-protected. Try a different image URL."
            )

    tools.register(
        name="send_image",
        description=(
            "Display an image or GIF inline in the chat. "
            "You MUST call this tool for EVERY image/GIF URL you want the user to see. "
            "Do NOT paste image URLs as plain text — they won't render. "
            "Instead, call send_image(url=...) so the picture appears visually. "
            "Works with direct image URLs (.jpg, .png, .gif, .webp) and GIF services (Giphy, Tenor)."
        ),
        handler=send_image,
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct URL to the image or GIF",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the image",
                    "default": "",
                },
            },
            "required": ["url"],
        },
    )

    # Sprint 8: Image generation builtin (optional — requires FAL_KEY or PINCER_GEMINI_API_KEY)
    _fal_key = settings.fal_key.get_secret_value()
    _gemini_key = settings.gemini_api_key.get_secret_value()
    if _fal_key or _gemini_key:
        from pincer.image.router import build_router_from_settings
        from pincer.tools.builtin.image_gen import make_generate_image_handler

        _image_router = build_router_from_settings()
        tools.register(
            name="generate_image",
            description=(
                "Generate image(s) from a text prompt and send them directly to the user. "
                "Use for creating original images, illustrations, or artwork."
            ),
            handler=make_generate_image_handler(_image_router),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed text description of the image to generate",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption displayed with the image",
                        "default": "",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Image aspect ratio — 1:1 (default), 16:9, 9:16, 4:3, 3:4",
                        "default": "1:1",
                    },
                    "num_images": {
                        "type": "integer",
                        "description": "Number of images to generate (default 1, max 4)",
                        "default": 1,
                    },
                },
                "required": ["prompt"],
            },
        )
        console.print("[green]Image generation tool registered[/green]")

    # Sprint 4: Load skills and register their tools
    from pathlib import Path as _SkillPath

    from pincer.tools.skills.loader import SkillLoader
    from pincer.tools.skills.scanner import SkillScanner

    SkillScanner()
    skill_loader = SkillLoader(
        bundled_dir=_SkillPath("skills"),
        scanner=None,  # bundled skills are trusted, skip scanning
    )
    loaded_skills = await skill_loader.discover_and_load()

    def _wrap_skill_fn(sync_fn):
        """Create an async handler wrapping a synchronous skill function."""
        import functools
        import inspect
        import json as _json

        async def handler(**kwargs):
            kwargs.pop("context", None)
            if inspect.iscoroutinefunction(sync_fn):
                result = await sync_fn(**kwargs)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, functools.partial(sync_fn, **kwargs))
            return _json.dumps(result) if isinstance(result, dict) else str(result)

        return handler

    all_fns = skill_loader.get_all_tool_functions()
    for schema in skill_loader.get_all_tool_schemas():
        fn_key = schema["name"].replace("__", ".", 1)
        fn = all_fns.get(fn_key)
        if fn:
            tools.register(
                name=schema["name"],
                description=schema["description"],
                handler=_wrap_skill_fn(fn),
                parameters=schema["input_schema"],
            )

    if loaded_skills:
        skill_names = [s.manifest.name for s in loaded_skills.values()]
        console.print(f"[green]Skills loaded: {', '.join(skill_names)}[/green]")

    # MCP client manager (optional — requires mcp package + pincer.toml or env config)
    mcp_manager = None
    try:
        from pincer.mcp import MCPClientManager, load_mcp_config

        mcp_cfg = load_mcp_config()
        if mcp_cfg.enabled and mcp_cfg.servers:
            mcp_manager = MCPClientManager(
                config=mcp_cfg,
                tool_registry=tools,
                audit_logger=audit_logger,
            )
            connection_results = await mcp_manager.start()
            for server_name, ok in connection_results.items():
                if ok:
                    console.print(f"[green]MCP '{server_name}' connected[/green]")
                else:
                    console.print(f"[yellow]MCP '{server_name}' failed to connect — skipping[/yellow]")
            if mcp_manager:
                total_mcp_tools = mcp_manager.total_tool_count()
                if total_mcp_tools > 0:
                    console.print(f"[green]MCP tools registered: {total_mcp_tools}[/green]")
                if total_mcp_tools > 100:
                    console.print(
                        "[yellow]Warning: >100 MCP tools registered — "
                        "LLM tool selection may degrade. Consider filtering.[/yellow]"
                    )
    except ImportError:
        pass  # mcp package not installed — silently skip
    except Exception as e:
        console.print(f"[yellow]MCP startup error: {e}[/yellow]")

    # Create agent
    agent = Agent(
        settings=settings,
        llm=llm,
        session_manager=session_mgr,
        cost_tracker=cost_tracker,
        tool_registry=tools,
        memory_store=memory_store,
        summarizer=summarizer,
    )
    if mcp_manager:
        agent.mcp_manager = mcp_manager

    # MCP server export (optional — exposes Pincer tools to external MCP clients)
    mcp_server = None

    # Message handler bridge
    async def on_message(incoming: IncomingMessage) -> str:
        from pincer.exceptions import RateLimitExceeded

        # Special commands
        if incoming.text == "/clear":
            session = await session_mgr.get_or_create(incoming.user_id, incoming.channel)
            await session_mgr.clear(session)
            return "Conversation cleared."

        if incoming.text == "/cost":
            summary = await cost_tracker.get_summary()
            today = await cost_tracker.get_today_spend()
            return (
                f"*Cost Summary*\n\n"
                f"Today: ${today:.4f}\n"
                f"Total: ${summary.total_usd:.4f}\n"
                f"Calls: {summary.total_calls}\n"
                f"Tokens: {summary.total_input_tokens:,} in / "
                f"{summary.total_output_tokens:,} out\n"
                f"Budget: ${settings.daily_budget_usd:.2f}/day"
            )

        # Sprint 5: Rate limit check
        try:
            await rate_limiter.check_message(incoming.user_id)
        except RateLimitExceeded as e:
            if audit_logger:
                await audit_logger.log(
                    AuditEntry(
                        user_id=incoming.user_id,
                        action=AuditAction.RATE_LIMIT_HIT,
                        channel=incoming.channel,
                        input_summary=e.message,
                    )
                )
            return e.message

        # Sprint 5: Audit incoming message
        if audit_logger:
            await audit_logger.log(
                AuditEntry(
                    user_id=incoming.user_id,
                    action=AuditAction.MESSAGE_RECEIVED,
                    channel=incoming.channel,
                    input_summary=(incoming.text or "")[:500],
                )
            )

        # Handle voice notes via Whisper transcription
        text = incoming.text
        if incoming.has_voice and incoming.voice_data:
            from pincer.tools.builtin.transcribe import transcribe_voice

            openai_key = settings.openai_api_key.get_secret_value()
            text = await transcribe_voice(
                audio_data=incoming.voice_data,
                mime_type=incoming.voice_mime or "audio/ogg",
                api_key=openai_key,
            )
            if not text or text.startswith("["):
                return text or "[Could not transcribe voice note]"

        # Handle file attachments — decode text files, save all to workspace
        if incoming.has_files:
            text_extensions = {
                ".txt",
                ".py",
                ".js",
                ".ts",
                ".json",
                ".csv",
                ".md",
                ".log",
                ".xml",
                ".html",
                ".css",
                ".yaml",
                ".yml",
                ".toml",
                ".ini",
                ".cfg",
                ".sh",
                ".bash",
                ".sql",
                ".rs",
                ".go",
                ".java",
                ".c",
                ".cpp",
                ".h",
                ".rb",
                ".php",
                ".swift",
                ".kt",
                ".env",
                ".gitignore",
            }
            uploads_dir = settings.data_dir / "workspace" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)

            file_parts: list[str] = []
            for raw_bytes, mime, filename in incoming.files:
                save_path = uploads_dir / filename
                save_path.write_bytes(raw_bytes)
                abs_path = str(save_path)

                ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                is_text = (
                    mime.startswith("text/")
                    or mime in ("application/json", "application/xml", "application/x-yaml")
                    or ext in text_extensions
                )
                if is_text:
                    try:
                        content = raw_bytes.decode("utf-8", errors="replace")
                        max_chars = 30_000
                        if len(content) > max_chars:
                            content = content[:max_chars] + f"\n... [truncated, {len(raw_bytes)} bytes total]"
                        file_parts.append(f"[File: {filename}]\n```\n{content}\n```")
                    except Exception:
                        file_parts.append(
                            f"[File: {filename}] saved to {abs_path} (binary, {len(raw_bytes)} bytes, {mime})"
                        )
                elif ext == ".pdf":
                    try:
                        import pymupdf

                        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
                        pages = [page.get_text() for page in doc]
                        doc.close()
                        content = "\n\n".join(pages)
                        max_chars = 30_000
                        if len(content) > max_chars:
                            content = content[:max_chars] + f"\n... [truncated, {len(pages)} pages total]"
                        file_parts.append(
                            f"[File: {filename} — {len(pages)} pages, saved to {abs_path}]\n```\n{content}\n```"
                        )
                    except ImportError:
                        file_parts.append(
                            f"[File: {filename}] saved to {abs_path} "
                            f"({len(raw_bytes)} bytes, {mime}). "
                            f"PDF text extraction unavailable (install pymupdf)."
                        )
                    except Exception as exc:
                        file_parts.append(
                            f"[File: {filename}] saved to {abs_path} "
                            f"({len(raw_bytes)} bytes, {mime}). "
                            f"PDF extraction failed: {exc}"
                        )
                else:
                    file_parts.append(
                        f"[File: {filename}] saved to {abs_path} "
                        f"({len(raw_bytes)} bytes, {mime}). "
                        f"Use shell_exec to process it with the absolute path above."
                    )

            file_context = "\n\n".join(file_parts)
            text = f"{file_context}\n\n{text}" if text else file_context

        response = await agent.handle_message(
            user_id=incoming.user_id,
            channel=incoming.channel,
            text=text,
            images=incoming.images if incoming.images else None,
        )

        cost_str = f"\n\n`${response.cost_usd:.4f}`" if response.cost_usd > 0 else ""
        return response.text + cost_str

    # Sprint 3: Identity resolver
    from pincer.core.identity import IdentityResolver

    identity = IdentityResolver(settings.db_path, settings.identity_map)
    await identity.ensure_table()
    await identity.seed_from_config()

    # Pending ask_user futures per user_id — populated by channels that route
    # ask_user responses back through on_message (WhatsApp).  Telegram uses
    # inline buttons so it owns its own pending state.
    _pending_ask: dict[str, asyncio.Future[str]] = {}

    _orig_on_message_fn = on_message

    async def on_message(incoming: IncomingMessage) -> str:  # type: ignore[no-redef]
        fut = _pending_ask.get(incoming.user_id)
        if fut and not fut.done():
            fut.set_result(incoming.text)
            return ""
        return await _orig_on_message_fn(incoming)

    # Start channels
    channels: list[BaseChannel] = []
    tg = None
    if settings.telegram_bot_token.get_secret_value():
        from pincer.channels.telegram import TelegramChannel

        tg = TelegramChannel(settings)
        tg.set_stream_agent(agent)
        tg.set_identity_resolver(identity)
        await tg.start(on_message)
        channels.append(tg)
        channel_map[tg.name] = tg

        console.print("[green]Telegram connected (streaming enabled)[/green]")

    # Sprint 3: Channel router for proactive delivery
    from pincer.channels.base import ChannelType
    from pincer.channels.router import ChannelRouter

    router = ChannelRouter(identity)
    if tg:
        router.register(ChannelType.TELEGRAM, tg)

    # Sprint 3: WhatsApp channel (optional)
    wa = None
    if settings.whatsapp_enabled:
        try:
            from pincer.channels.whatsapp import WhatsAppChannel

            wa = WhatsAppChannel(settings)
            wa.set_identity_resolver(identity)
            await wa.start(on_message)
            channels.append(wa)
            channel_map[wa.name] = wa
            router.register(ChannelType.WHATSAPP, wa)
            console.print("[green]WhatsApp connected[/green]")
        except Exception as e:
            console.print(f"[yellow]WhatsApp failed: {e}[/yellow]")

    # Wire approval + ask_user callbacks across all interactive channels.
    async def _channel_approval(
        tool_name: str,
        arguments: dict,
        user_id: str,
        channel: str,
    ) -> bool:
        if channel == "telegram" and tg is not None:
            return await tg.request_approval(user_id, tool_name, arguments)
        if channel == "whatsapp" and wa is not None:
            return await wa.request_approval(user_id, tool_name, arguments)
        logger.warning(
            "No approval UI for channel %s; denying %s",
            channel,
            tool_name,
        )
        return False

    async def _channel_ask_user(user_id: str, channel: str, question: str) -> str:
        if channel == "whatsapp" and wa is not None:
            return await wa.request_input(
                user_id,
                f"🔌 *MCP Client* asks:\n\n{question}\n\n_Reply with your answer:_",
            )
        if channel == "telegram" and tg is not None:
            fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            _pending_ask[user_id] = fut
            try:
                await tg.send(
                    user_id,
                    f"🔌 **MCP Client** asks:\n\n{question}\n\n_Reply with your answer:_",
                )
                return await asyncio.wait_for(fut, timeout=120)
            except TimeoutError:
                return "[No response from user — timed out after 120s]"
            finally:
                _pending_ask.pop(user_id, None)
        return "[No supported channel for ask_user]"

    if tg is not None or wa is not None:
        agent._approval_callback = _channel_approval
        agent._ask_user_callback = _channel_ask_user

    # Sprint 7: Voice channel (optional)
    # Start when either inbound (voice_enabled) or outbound (voice_outbound_enabled) is enabled
    vc = None
    if (settings.voice_enabled or settings.voice_outbound_enabled) and settings.twilio_account_sid:
        try:
            from pincer.channels.phone_calls import VoiceChannel
            from pincer.voice.engine import get_voice_engine
            from pincer.voice.outbound import make_phone_call
            from pincer.voice.twiml_server import init_voice_routes

            voice_engine = get_voice_engine(settings)
            vc = VoiceChannel(settings)
            vc.set_engine(voice_engine)
            vc.set_identity_resolver(identity)
            await vc.start(on_message)
            channels.append(vc)
            channel_map[vc.name] = vc
            router.register(ChannelType.VOICE, vc)
            init_voice_routes(voice_engine, settings)

            if settings.voice_outbound_enabled:
                tools.register(
                    name="make_phone_call",
                    description=(
                        "Place a real phone call to a number. REQUIRED when the user asks you to call someone: "
                        "you MUST call this tool with target_number (E.164) and purpose. "
                        "Do NOT describe or simulate a call in text. Do NOT output XML or structured call blocks. "
                        "Only this tool can place calls."
                    ),
                    handler=make_phone_call,
                    parameters={
                        "type": "object",
                        "properties": {
                            "target_number": {
                                "type": "string",
                                "description": "Phone number in E.164 format (e.g. +14155551234)",
                            },
                            "purpose": {
                                "type": "string",
                                "description": "What the call is about",
                            },
                            "instructions": {
                                "type": "string",
                                "description": "Specific instructions for the agent during the call",
                                "default": "",
                            },
                            "max_duration": {
                                "type": "integer",
                                "description": "Maximum call duration in seconds (default 300)",
                                "default": 300,
                            },
                        },
                        "required": ["target_number", "purpose"],
                    },
                    require_approval=True,
                )

            console.print(f"[green]Voice calling enabled ({settings.voice_engine})[/green]")
        except Exception as e:
            console.print(f"[yellow]Voice setup failed: {e}[/yellow]")
    else:
        if settings.voice_enabled or settings.voice_outbound_enabled:
            console.print("[yellow]Voice/outbound enabled but PINCER_TWILIO_ACCOUNT_SID not set[/yellow]")
    if settings.voice_outbound_enabled and not settings.voice_webhook_base_url:
        console.print(
            "[yellow]Outbound calling enabled but PINCER_VOICE_WEBHOOK_BASE_URL not set — "
            "calls will fail until configured.[/yellow]"
        )
    if settings.voice_enabled and not settings.voice_outbound_enabled:
        console.print(
            "[dim]Voice outbound disabled — set PINCER_VOICE_OUTBOUND_ENABLED=true for 'Call X' from text.[/dim]"
        )

    # Sprint 4: Discord channel (optional)
    dc = None
    if settings.discord_bot_token.get_secret_value():
        try:
            from pincer.channels.discord_channel import DiscordChannel

            dc = DiscordChannel(settings)
            dc.set_identity_resolver(identity)
            dc.set_agent(agent)
            await dc.start(on_message)
            channels.append(dc)
            channel_map[dc.name] = dc
            router.register(ChannelType.DISCORD, dc)
            console.print("[green]Discord connected[/green]")
        except Exception as e:
            console.print(f"[yellow]Discord failed: {e}[/yellow]")
    else:
        console.print("[dim]Discord skipped (no PINCER_DISCORD_BOT_TOKEN)[/dim]")

    # Sprint 7.5: Signal channel (optional)
    sig = None
    if settings.signal_enabled:
        if settings.signal_phone_number:
            try:
                from pincer.channels.signal import SignalChannel

                sig = SignalChannel(settings)
                sig.set_identity_resolver(identity)
                await sig.start(on_message)
                channels.append(sig)
                channel_map[sig.name] = sig
                router.register(ChannelType.SIGNAL, sig)
                console.print("[green]Signal connected[/green]")
            except Exception as e:
                console.print(f"[yellow]Signal failed: {e}[/yellow]")
        else:
            console.print("[yellow]Signal enabled but PINCER_SIGNAL_PHONE_NUMBER not set[/yellow]")

    # Slack channel (optional — requires PINCER_SLACK_BOT_TOKEN + PINCER_SLACK_APP_TOKEN)
    slk = None
    if settings.slack_bot_token.get_secret_value() and settings.slack_app_token.get_secret_value():
        try:
            from pincer.channels.slack import SlackChannel

            slk = SlackChannel(settings)
            slk.set_identity_resolver(identity)
            await slk.start(on_message)
            if slk._app is not None:  # start() may bail silently if import fails
                channels.append(slk)
                channel_map[slk.name] = slk
                router.register(ChannelType.SLACK, slk)
                console.print("[green]Slack connected (Socket Mode)[/green]")
            else:
                console.print("[yellow]Slack failed to start (check logs)[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Slack failed: {e}[/yellow]")
    else:
        console.print("[dim]Slack skipped (no PINCER_SLACK_BOT_TOKEN / PINCER_SLACK_APP_TOKEN)[/dim]")

    if not channels:
        console.print("[yellow]No channels configured. Set PINCER_TELEGRAM_BOT_TOKEN.[/yellow]")
        return

    # Sprint 3: Scheduler + Proactive Agent
    from pincer.scheduler import CronScheduler, EventTriggerManager, ProactiveAgent

    proactive = ProactiveAgent(settings.db_path)
    await proactive.ensure_table()

    scheduler = CronScheduler(settings.db_path, router)
    scheduler.register_action("briefing", proactive.generate_briefing)
    scheduler.register_action("custom", proactive.run_custom_action)
    await scheduler.start()

    # Sprint 3: Event triggers
    triggers = EventTriggerManager(settings.db_path, router)
    await triggers.start()

    # Auto-create default morning briefing if configured
    if settings.default_user_id and settings.briefing_time:
        try:
            hour, minute = settings.briefing_time.split(":")
            existing = await scheduler.list_schedules(settings.default_user_id)
            if not any(s["name"] == "morning_briefing" for s in existing):
                await scheduler.add(
                    name="morning_briefing",
                    cron_expr=f"{minute} {hour} * * *",
                    action={"type": "briefing"},
                    pincer_user_id=settings.default_user_id,
                    tz=settings.briefing_timezone,
                )
                console.print(
                    f"[green]Morning briefing scheduled at {settings.briefing_time} "
                    f"({settings.briefing_timezone})[/green]"
                )
        except Exception as e:
            console.print(f"[yellow]Briefing schedule error: {e}[/yellow]")

    # MCP server export (Sprint 4: expose Pincer tools to Claude Desktop / Cursor / etc.)
    try:
        from pincer.mcp import PincerMCPServer
        from pincer.mcp import load_mcp_config as _load_mcp_cfg

        _mcp_full_cfg = _load_mcp_cfg()
        if _mcp_full_cfg.server.enabled:
            _acb = agent._approval_callback
            mcp_server = PincerMCPServer(
                config=_mcp_full_cfg.server,
                tool_registry=tools,
                approval_callback=((lambda tn, args: _acb(tn, args, "", "")) if _acb else None),
                ask_user_callback=(
                    (lambda q: agent._ask_user_on_active_channel(q)) if agent._ask_user_callback else None
                ),
            )
            agent.mcp_server = mcp_server
            await mcp_server.start()
            console.print(
                f"[green]MCP server started at {mcp_server.endpoint} ({mcp_server.exposed_tool_count} tool(s))[/green]"
            )
    except Exception as e:
        console.print(f"[yellow]MCP server startup error: {e}[/yellow]")

    # Sprint 5: Start API server
    api_server = None
    api_task = None
    if _port_in_use(settings.dashboard_host, settings.dashboard_port):
        console.print(f"[yellow]Port {settings.dashboard_port} is already in use. API server skipped.[/yellow]")
        console.print(
            f"  Options: Set PINCER_DASHBOARD_PORT=8081 in .env, "
            f"or kill the process: lsof -i :{settings.dashboard_port}"
        )
    else:
        try:
            import uvicorn

            from pincer.api.server import create_app

            api_app = create_app()
            api_config = uvicorn.Config(
                api_app,
                host=settings.dashboard_host,
                port=settings.dashboard_port,
                log_level="warning",
            )
            api_server = uvicorn.Server(api_config)
            api_task = asyncio.create_task(api_server.serve())
            console.print(
                f"[green]API server started on http://{settings.dashboard_host}:{settings.dashboard_port}[/green]"
            )
        except Exception as e:
            console.print(f"[yellow]API server failed to start: {e}[/yellow]")

    active = [ch.name for ch in channels]
    console.print(
        f"\n[bold green]{settings.agent_name} is running![/bold green] "
        f"Channels: {', '.join(active)}. Press Ctrl+C to stop.\n"
    )

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/yellow]")
    finally:
        if api_server:
            api_server.should_exit = True
            if api_task:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(api_task, timeout=3.0)
        if mcp_manager:
            await mcp_manager.stop()
        if mcp_server:
            await mcp_server.stop()
        await triggers.stop()
        await scheduler.stop()
        await proactive.close()
        for ch in channels:
            await ch.stop()
        try:
            from pincer.tools.builtin.browser import close_browser

            await close_browser()
        except ImportError:
            pass
        await llm.close()
        await session_mgr.close()
        await cost_tracker.close()
        if memory_store:
            await memory_store.close()
        if audit_logger:
            await audit_logger.shutdown()
        console.print("[green]Shutdown complete[/green]")
        # Cancel any lingering asyncio tasks (e.g. in-flight LLM calls from
        # channel update handlers) before exiting so the process doesn't hang.
        _pending = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        for _t in _pending:
            _t.cancel()
        os._exit(0)


@app.command()
def config() -> None:
    """Show current configuration."""
    from pincer.config import get_settings

    try:
        s = get_settings()
        console.print("[bold]Pincer Configuration[/bold]\n")
        console.print(f"  Provider:     {s.default_provider.value}")
        console.print(f"  Model:        {s.default_model}")
        console.print(f"  Anthropic:    {'set' if s.anthropic_api_key.get_secret_value() else 'not set'}")
        console.print(f"  OpenAI:       {'set' if s.openai_api_key.get_secret_value() else 'not set'}")
        console.print(f"  Telegram:     {'set' if s.telegram_bot_token.get_secret_value() else 'not set'}")
        console.print(f"  Budget:       ${s.daily_budget_usd:.2f}/day")
        console.print(f"  Data dir:     {s.data_dir}")
        console.print(f"  Shell:        {'enabled' if s.shell_enabled else 'disabled'}")
        console.print(f"  Log level:    {s.log_level.value}")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@app.command()
def cost(
    days: int = typer.Option(0, "--days", help="Show spending for last N days"),
    by_model: bool = typer.Option(False, "--by-model", help="Breakdown by LLM model"),
    by_tool: bool = typer.Option(False, "--by-tool", help="Breakdown by tool"),
    export: str = typer.Option("", "--export", help="Export cost data to JSON file"),
) -> None:
    """Show API costs and spending breakdown."""
    asyncio.run(_show_cost(days=days, by_model=by_model, by_tool=by_tool, export=export))


async def _show_cost(days: int = 0, by_model: bool = False, by_tool: bool = False, export: str = "") -> None:
    from datetime import datetime, timedelta

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.llm.cost_tracker import CostTracker

    s = get_settings_relaxed()
    tracker = CostTracker(s.db_path, s.daily_budget_usd)
    await tracker.initialize()

    today = await tracker.get_today_spend()
    summary = await tracker.get_summary()

    console.print("[bold]Pincer Cost Report[/bold]\n")
    console.print(f"  Today:   ${today:.4f} / ${s.daily_budget_usd:.2f}")
    console.print(f"  Total:   ${summary.total_usd:.4f} ({summary.total_calls} calls)")
    console.print(f"  Tokens:  {summary.total_input_tokens:,} in / {summary.total_output_tokens:,} out")

    if days > 0:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        history = await tracker.get_daily_history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if history:
            console.print(f"\n[bold]Last {days} days:[/bold]")
            table = Table()
            table.add_column("Date")
            table.add_column("Cost", justify="right")
            table.add_column("Requests", justify="right")
            for entry in history:
                table.add_row(entry["date"], f"${entry['total']:.4f}", str(entry["requests"]))
            console.print(table)

    if by_model:
        end = datetime.now(UTC)
        start = end - timedelta(days=max(days, 7))
        models = await tracker.get_costs_by_model(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if models:
            console.print("\n[bold]By Model:[/bold]")
            table = Table()
            table.add_column("Model")
            table.add_column("Cost", justify="right")
            table.add_column("Requests", justify="right")
            table.add_column("Tokens", justify="right")
            for m in models:
                table.add_row(m["model"], f"${m['total']:.4f}", str(m["requests"]), f"{m['tokens']:,}")
            console.print(table)

    if export:
        import json as _json
        from pathlib import Path as _P

        end = datetime.now(UTC)
        start = end - timedelta(days=max(days, 30))
        history = await tracker.get_daily_history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        _P(export).write_text(
            _json.dumps(
                {
                    "history": history,
                    "summary": {
                        "total_usd": summary.total_usd,
                        "total_calls": summary.total_calls,
                    },
                },
                indent=2,
            )
        )
        console.print(f"\n[green]Exported to {export}[/green]")

    await tracker.close()


@app.command(name="pair-whatsapp")
def pair_whatsapp() -> None:
    """Pair WhatsApp via QR code (run once to link your device)."""
    asyncio.run(_pair_whatsapp())


async def _pair_whatsapp() -> None:
    from pincer.config import get_settings

    settings = get_settings()
    console.print("[bold]WhatsApp Pairing[/bold]\n")
    console.print("This will display a QR code. Scan it with:")
    console.print("  WhatsApp -> Settings -> Linked Devices -> Link a Device\n")

    try:
        from pincer.channels.whatsapp import WhatsAppChannel

        wa = WhatsAppChannel(settings)

        async def noop_handler(msg):  # type: ignore[no-untyped-def]
            return "Pairing mode — send messages after running `pincer run`."

        await wa.start(noop_handler)
        console.print("\n[green]WhatsApp paired successfully![/green]")
        console.print("Session saved. Run `pincer run` with PINCER_WHATSAPP_ENABLED=true.")
        await wa.stop()
    except Exception as e:
        console.print(f"[red]Pairing failed: {e}[/red]")


@app.command(name="auth-google")
def auth_google() -> None:
    """Run Google Calendar OAuth consent flow (one-time setup)."""

    from pincer.config import get_settings
    from pincer.tools.builtin.calendar_tool import SCOPES

    settings = get_settings()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_token.json"

    console.print("[bold]Google Calendar — OAuth Setup[/bold]\n")

    if not credentials_path.exists():
        console.print(f"[red]Missing: {credentials_path}[/red]")
        console.print(
            "\nDownload the OAuth client JSON from:\n"
            "  Google Cloud Console -> APIs & Services -> Credentials\n"
            "  -> OAuth 2.0 Client IDs -> Download JSON\n"
            f"\nSave it as: {credentials_path}"
        )
        raise typer.Exit(1)

    if token_path.exists():
        console.print(f"[yellow]Token already exists: {token_path}[/yellow]")
        if not typer.confirm("Overwrite existing token?"):
            raise typer.Exit(0)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        console.print("[red]google-auth-oauthlib is not installed.[/red]\nRun:  uv pip install google-auth-oauthlib")
        raise typer.Exit(1) from None

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)

    console.print("Opening browser for Google consent...\n")
    try:
        creds = flow.run_local_server(port=0)
    except Exception:
        console.print(
            "[yellow]Browser not available. Trying manual flow...[/yellow]\n"
            "Open the URL below in any browser, then paste the code back here.\n"
        )
        creds = flow.run_local_server(port=8080, open_browser=False)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    console.print("\n[green]Google Calendar authorized![/green]")
    console.print(f"  Token saved to: {token_path}")
    console.print(f"  Refresh token:  {'Yes' if creds.refresh_token else 'No'}")
    console.print(f"  Expires:        {creds.expiry}")
    console.print("\nYou can now use calendar tools in Pincer.")


# ═══════════════════════════════════════════════
# Sprint 4: New commands
# ═══════════════════════════════════════════════


@app.command(name="init")
def init() -> None:
    """Interactive setup wizard — zero to running in 5 minutes."""
    from pathlib import Path as _P

    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt

    console.print(Panel("[bold]Pincer Setup Wizard[/bold]", expand=False))

    env_lines: list[str] = []

    # Step 1: LLM Provider
    console.print("\n[bold]Step 1: LLM Provider[/bold]")
    provider = Prompt.ask(
        "Choose provider",
        choices=["anthropic", "openai", "both"],
        default="anthropic",
    )
    if provider in ("anthropic", "both"):
        key = Prompt.ask("Anthropic API key", password=True)
        env_lines.append(f"PINCER_ANTHROPIC_API_KEY={key}")
        if provider == "anthropic":
            env_lines.append("PINCER_DEFAULT_PROVIDER=anthropic")
            env_lines.append("PINCER_DEFAULT_MODEL=claude-sonnet-4-5-20250929")
    if provider in ("openai", "both"):
        key = Prompt.ask("OpenAI API key", password=True)
        env_lines.append(f"PINCER_OPENAI_API_KEY={key}")
        if provider == "openai":
            env_lines.append("PINCER_DEFAULT_PROVIDER=openai")
            env_lines.append("PINCER_DEFAULT_MODEL=gpt-4o")

    # Step 2: Channels
    console.print("\n[bold]Step 2: Channels[/bold]")
    if Confirm.ask("Enable Telegram?", default=False):
        token = Prompt.ask("Telegram bot token", password=True)
        env_lines.append(f"PINCER_TELEGRAM_BOT_TOKEN={token}")
        allowed = Prompt.ask("Allowed user IDs (comma-separated, empty = all)", default="")
        if allowed:
            env_lines.append(f"PINCER_TELEGRAM_ALLOWED_USERS={allowed}")

    if Confirm.ask("Enable Discord?", default=False):
        token = Prompt.ask("Discord bot token", password=True)
        env_lines.append(f"PINCER_DISCORD_BOT_TOKEN={token}")

    if Confirm.ask("Enable WhatsApp?", default=False):
        env_lines.append("PINCER_WHATSAPP_ENABLED=true")
        console.print("  Run [bold]pincer pair-whatsapp[/bold] to pair after setup.")

    if Confirm.ask("Enable Signal?", default=False):
        env_lines.append("PINCER_SIGNAL_ENABLED=true")
        phone = Prompt.ask("Signal phone number (E.164, e.g. +491234567890)")
        env_lines.append(f"PINCER_SIGNAL_PHONE_NUMBER={phone}")
        allowlist = Prompt.ask("Allowed DM numbers (comma-separated, empty = allow all)", default="")
        if allowlist:
            env_lines.append(f"PINCER_SIGNAL_ALLOWLIST={allowlist}")
        api_url = Prompt.ask(
            "signal-cli-rest-api URL (for Docker: http://signal-api:8080)",
            default="http://signal-api:8080",
        )
        env_lines.append(f"PINCER_SIGNAL_API_URL={api_url}")
        console.print(
            "  Start signal-api: [bold]docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d[/bold]"
        )
        console.print("  Then pair: [bold]pincer signal pair[/bold] (opens 127.0.0.1:8081 in browser automatically)")

    # Step 3: Preferences
    console.print("\n[bold]Step 3: Preferences[/bold]")
    tz = Prompt.ask("Timezone", default="Europe/Berlin")
    env_lines.append(f"PINCER_TIMEZONE={tz}")
    budget = Prompt.ask("Daily budget (USD)", default="5.00")
    env_lines.append(f"PINCER_DAILY_BUDGET_USD={budget}")

    # Step 4: Voice Calling
    console.print("\n[bold]Step 4: Voice Calling[/bold]")
    if Confirm.ask("Enable voice calling (Twilio)?", default=False):
        env_lines.append("PINCER_VOICE_ENABLED=true")
        sid = Prompt.ask("Twilio Account SID")
        env_lines.append(f"PINCER_TWILIO_ACCOUNT_SID={sid}")
        token = Prompt.ask("Twilio Auth Token", password=True)
        env_lines.append(f"PINCER_TWILIO_AUTH_TOKEN={token}")
        phone = Prompt.ask("Twilio phone number (E.164, e.g. +14155551234)")
        env_lines.append(f"PINCER_TWILIO_PHONE_NUMBER={phone}")
        webhook = Prompt.ask("Public webhook URL (https://...)", default="")
        if webhook:
            env_lines.append(f"PINCER_VOICE_WEBHOOK_BASE_URL={webhook}")
        if Confirm.ask("Enable outbound calling?", default=False):
            env_lines.append("PINCER_VOICE_OUTBOUND_ENABLED=true")

    # Step 5: Optional Integrations
    console.print("\n[bold]Step 5: Optional Integrations[/bold]")
    if Confirm.ask("Configure email?", default=False):
        env_lines.append(f"PINCER_EMAIL_IMAP_HOST={Prompt.ask('IMAP host', default='imap.gmail.com')}")
        env_lines.append(f"PINCER_EMAIL_SMTP_HOST={Prompt.ask('SMTP host', default='smtp.gmail.com')}")
        env_lines.append(f"PINCER_EMAIL_USERNAME={Prompt.ask('Email username')}")
        env_lines.append(f"PINCER_EMAIL_PASSWORD={Prompt.ask('Email password', password=True)}")

    if Confirm.ask("Add OpenWeatherMap key?", default=False):
        key = Prompt.ask("OpenWeatherMap API key", password=True)
        env_lines.append(f"PINCER_OPENWEATHERMAP_API_KEY={key}")

    if Confirm.ask("Add NewsAPI key?", default=False):
        key = Prompt.ask("NewsAPI key", password=True)
        env_lines.append(f"PINCER_NEWSAPI_KEY={key}")

    # Write .env
    env_path = _P(".env")
    if env_path.exists() and not Confirm.ask(f"\n{env_path} already exists. Overwrite?", default=False):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(0)

    env_path.write_text("\n".join(env_lines) + "\n")

    console.print(
        Panel(
            "[green]Setup complete![/green]\n\n"
            "Next steps:\n"
            "  1. [bold]pincer run[/bold]   — start the agent\n"
            "  2. [bold]pincer doctor[/bold] — verify configuration\n"
            "  3. [bold]pincer chat[/bold]  — test in the terminal",
            title="Done",
            expand=False,
        )
    )


@app.command()
def doctor(
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run 25+ security checks with traffic-light report."""
    import json as _json
    from pathlib import Path as _P

    from pincer.security.doctor import CheckStatus, SecurityDoctor

    doc = SecurityDoctor(
        data_dir=_P("data"),
        config_dir=_P("."),
    )
    report = doc.run_all()

    if output_json:
        console.print(_json.dumps(report.to_dict(), indent=2))
        return

    from rich.table import Table

    status_icons = {
        CheckStatus.PASS: "[green]\u2705[/green]",
        CheckStatus.WARNING: "[yellow]\u26a0\ufe0f[/yellow]",
        CheckStatus.CRITICAL: "[red]\u274c[/red]",
        CheckStatus.SKIPPED: "[dim]\u2796[/dim]",
    }

    console.print(
        f"\n[bold]Pincer Security Doctor[/bold]  "
        f"Score: [{'green' if report.score >= 80 else 'yellow' if report.score >= 60 else 'red'}]"
        f"{report.score}/100[/]\n"
    )

    current_category = ""
    table = Table(show_header=True)
    table.add_column("", width=4)
    table.add_column("Check", style="bold")
    table.add_column("Message")
    table.add_column("Fix", style="dim")

    for check in report.checks:
        if check.category != current_category:
            current_category = check.category
            table.add_row("", f"[bold underline]{current_category.upper()}[/bold underline]", "", "")
        table.add_row(
            status_icons.get(check.status, ""),
            check.name,
            check.message,
            check.fix_hint,
        )

    console.print(table)
    console.print(
        f"\n  [green]{report.passed} passed[/green]  "
        f"[yellow]{report.warnings} warnings[/yellow]  "
        f"[red]{report.critical} critical[/red]\n"
    )


@app.command()
def chat() -> None:
    """Interactive CLI chat — test the agent without messaging apps."""
    asyncio.run(_chat_loop())


async def _chat_loop() -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel

    from pincer.config import get_settings

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        return

    _setup_logging("WARNING")

    console.print(
        Panel(
            f"[bold]{settings.agent_name} CLI Chat[/bold]\n"
            "Type your message and press Enter. Commands: /quit, /clear, /cost",
            expand=False,
        )
    )

    # Reuse the same agent setup as `run` but minimal
    from pincer.core.agent import Agent
    from pincer.core.session import SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.memory.store import MemoryStore
    from pincer.memory.summarizer import Summarizer
    from pincer.tools.builtin.files import file_list, file_read, file_write
    from pincer.tools.builtin.web_search import web_search
    from pincer.tools.registry import ToolRegistry

    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()
    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    if settings.default_provider.value == "anthropic":
        from pincer.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(settings)
    elif settings.default_provider.value == "grok":
        from pincer.llm.grok_provider import GrokProvider

        llm = GrokProvider(settings)
    else:
        from pincer.llm.openai_provider import OpenAIProvider

        llm = OpenAIProvider(settings)

    memory_store: MemoryStore | None = None
    summarizer: Summarizer | None = None
    if settings.memory_enabled:
        memory_store = MemoryStore(settings.db_path)
        await memory_store.initialize()
        summarizer = Summarizer(
            llm=llm,
            memory_store=memory_store,
            session_manager=session_mgr,
            summary_model=settings.summary_model,
            threshold=settings.summary_threshold,
        )

    tools = ToolRegistry()
    tools.register(
        name="web_search",
        description="Search the web",
        handler=web_search,
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    tools.register(name="file_read", description="Read a file", handler=file_read)
    tools.register(name="file_write", description="Write a file", handler=file_write)
    tools.register(name="file_list", description="List files", handler=file_list)

    agent = Agent(
        settings=settings,
        llm=llm,
        session_manager=session_mgr,
        cost_tracker=cost_tracker,
        tool_registry=tools,
        memory_store=memory_store,
        summarizer=summarizer,
    )

    user_id = "cli_user"
    channel = "cli"

    try:
        while True:
            try:
                text = console.input("[bold cyan]You:[/bold cyan] ")
            except (EOFError, KeyboardInterrupt):
                break

            text = text.strip()
            if not text:
                continue
            if text.lower() in ("/quit", "exit", "quit"):
                break
            if text == "/clear":
                session = await session_mgr.get_or_create(user_id, channel)
                await session_mgr.clear(session)
                console.print("[dim]Conversation cleared.[/dim]")
                continue
            if text == "/cost":
                today = await cost_tracker.get_today_spend()
                console.print(f"[dim]Today: ${today:.4f}[/dim]")
                continue

            with console.status(f"[bold green]{settings.agent_name} is thinking...[/bold green]"):
                response = await agent.handle_message(
                    user_id=user_id,
                    channel=channel,
                    text=text,
                )

            console.print(f"\n[bold green]{settings.agent_name}:[/bold green]")
            try:
                console.print(Markdown(response.text))
            except Exception:
                console.print(response.text)
            if response.cost_usd > 0:
                console.print(f"[dim]${response.cost_usd:.4f}[/dim]")
            console.print()
    finally:
        await llm.close()
        await session_mgr.close()
        await cost_tracker.close()
        if memory_store:
            await memory_store.close()
        console.print("[dim]Goodbye.[/dim]")


# ── Signal subcommands ────────────────────────

signal_app = typer.Typer(name="signal", help="Manage Signal messenger integration")
app.add_typer(signal_app, name="signal")


@signal_app.command(name="pair")
def signal_pair() -> None:
    """Open the Signal QR-code link in your browser to register/link a device."""
    import urllib.error
    import urllib.request
    import webbrowser

    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    pair_url = settings.signal_pair_url.rstrip("/")
    qr_url = f"{pair_url}/v1/qrcodelink?device_name=Pincer"

    # Pre-flight: verify signal-api is reachable before opening browser
    try:
        req = urllib.request.Request(f"{pair_url}/v1/about", method="GET")
        with urllib.request.urlopen(req, timeout=3) as _:
            pass
    except (OSError, urllib.error.URLError) as e:
        console.print("[bold red]Cannot reach signal-api[/bold red]")
        console.print(f"  {pair_url} — {e}")
        console.print(
            "\n[dim]Start signal-api first (no build required):[/dim]\n"
            "  [bold]docker compose -f docker-compose.yml -f docker-compose.signal.yml "
            "up -d signal-api[/bold]\n"
        )
        raise typer.Exit(1) from e

    console.print(f"[bold]Signal Pairing[/bold]\n\nOpening QR link: {qr_url}")
    console.print("\nScan the QR code with Signal: Settings → Linked Devices → Link New Device")
    webbrowser.open(qr_url)


@signal_app.command(name="status")
def signal_status() -> None:
    """Check Signal API health and registered accounts."""
    asyncio.run(_signal_status())


async def _signal_status() -> None:
    from pincer.channels.signal_client import SignalAPIError, SignalClient
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    console.print("[bold]Signal Status[/bold]\n")
    console.print(f"  API URL:    {settings.signal_api_url}")
    console.print(f"  Phone:      {settings.signal_phone_number or '(not set)'}")
    console.print(f"  Enabled:    {settings.signal_enabled}")

    client = SignalClient(settings.signal_api_url, settings.signal_phone_number)
    try:
        await client.connect()
        try:
            health = await client.health()
            console.print(f"  Health:     [green]OK[/green] {health}")
        except SignalAPIError as e:
            console.print(f"  Health:     [red]FAIL[/red] {e}")

        accounts = await client.list_accounts()
        if accounts:
            console.print(f"  Accounts:   {', '.join(accounts)}")
        else:
            console.print("  Accounts:   (none registered yet — run `pincer signal pair`)")

        about = await client.about()
        console.print(f"  About:      {about}")
    finally:
        await client.disconnect()


@signal_app.command(name="test")
def signal_test(
    recipient: str = typer.Argument(..., help="Recipient phone number (E.164)"),
) -> None:
    """Send a test message via Signal."""
    asyncio.run(_signal_test(recipient))


async def _signal_test(recipient: str) -> None:
    from pincer.channels.signal_client import SignalAPIError, SignalClient
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    if not settings.signal_phone_number:
        console.print("[red]PINCER_SIGNAL_PHONE_NUMBER not set.[/red]")
        raise typer.Exit(1)

    client = SignalClient(settings.signal_api_url, settings.signal_phone_number)
    try:
        await client.connect()
        await client.send_message(recipient, "Hello from Pincer! Signal channel is working.")
        console.print(f"[green]Test message sent to {recipient}[/green]")
    except SignalAPIError as e:
        console.print(f"[red]Send failed: {e}[/red]")
    finally:
        await client.disconnect()


# ── Skills subcommands ────────────────────────

skills_app = typer.Typer(name="skills", help="Manage skills and plugins")
app.add_typer(skills_app, name="skills")


@skills_app.command(name="list")
def skills_list() -> None:
    """List installed skills."""
    from pathlib import Path as _P

    from rich.table import Table

    from pincer.tools.skills.loader import SkillLoader

    loader = SkillLoader(bundled_dir=_P("skills"))
    dirs = loader._discover_skill_dirs()

    table = Table(title="Installed Skills")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Tools")
    table.add_column("Author")
    table.add_column("Source")

    import json

    for d in dirs:
        try:
            m = json.loads((d / "manifest.json").read_text())
            tool_names = [t["name"] for t in m.get("tools", [])]
            source = "bundled" if "skills" in str(d) and ".pincer" not in str(d) else "user"
            table.add_row(
                m.get("name", d.name),
                m.get("version", "?"),
                str(len(tool_names)),
                m.get("author", "unknown"),
                source,
            )
        except Exception as e:
            table.add_row(d.name, "?", "?", "?", f"error: {e}")

    console.print(table)


@skills_app.command(name="install")
def skills_install(source: str = typer.Argument(help="Path to skill directory")) -> None:
    """Install a skill (scan first, block if unsafe)."""
    import json
    import shutil
    from pathlib import Path as _P

    from pincer.tools.skills.scanner import SkillScanner

    source_path = _P(source)
    if not source_path.is_dir():
        console.print(f"[red]Not a directory: {source}[/red]")
        raise typer.Exit(1)
    if not (source_path / "manifest.json").is_file() or not (source_path / "skill.py").is_file():
        console.print("[red]Invalid skill: needs manifest.json and skill.py[/red]")
        raise typer.Exit(1)

    scanner = SkillScanner()
    result = scanner.scan_directory(str(source_path))
    console.print(result.summary())

    if not result.passed:
        console.print(f"\n[red]Skill blocked (score {result.score}/100, min 50)[/red]")
        raise typer.Exit(1)

    manifest = json.loads((source_path / "manifest.json").read_text())
    name = manifest.get("name", source_path.name)
    dest = _P.home() / ".pincer" / "skills" / name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_path, dest, dirs_exist_ok=True)
    console.print(f"\n[green]Skill '{name}' installed to {dest}[/green]")


@skills_app.command(name="create")
def skills_create(name: str = typer.Argument(help="Name for the new skill")) -> None:
    """Scaffold a new skill directory."""
    import json
    from pathlib import Path as _P

    skill_dir = _P("skills") / name
    if skill_dir.exists():
        console.print(f"[red]Directory already exists: {skill_dir}[/red]")
        raise typer.Exit(1)

    skill_dir.mkdir(parents=True)

    manifest = {
        "name": name,
        "version": "0.1.0",
        "description": f"{name} skill",
        "author": "you",
        "permissions": [],
        "env_required": [],
        "tools": [
            {
                "name": f"{name}_action",
                "description": f"Main action for {name}",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "Input value"},
                    },
                    "required": ["input"],
                },
            }
        ],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    skill_py = f'''"""Skill: {name}"""


def {name}_action(input: str) -> dict:
    """Main action for {name}."""
    return {{"result": f"Processed: {{input}}"}}
'''
    (skill_dir / "skill.py").write_text(skill_py)
    console.print(f"[green]Created skill scaffold at {skill_dir}/[/green]")
    console.print("  manifest.json — edit metadata and tool definitions")
    console.print("  skill.py      — implement your tool functions")


@skills_app.command(name="scan")
def skills_scan(path: str = typer.Argument(help="Path to skill directory")) -> None:
    """Run security scanner on a skill."""
    from pathlib import Path as _P

    from rich.table import Table

    from pincer.tools.skills.scanner import SkillScanner

    skill_path = _P(path)
    if not skill_path.is_dir():
        console.print(f"[red]Not a directory: {path}[/red]")
        raise typer.Exit(1)

    scanner = SkillScanner()
    result = scanner.scan_directory(str(skill_path))

    console.print(f"\n[bold]Scan: {result.skill_name}[/bold]")
    console.print(f"Score: {result.score}/100 — {'[green]PASS[/green]' if result.passed else '[red]FAIL[/red]'}")

    if result.findings:
        table = Table(title="Findings")
        table.add_column("Sev", width=8)
        table.add_column("Line", width=6)
        table.add_column("Category")
        table.add_column("Description")
        table.add_column("Penalty", justify="right")

        for f in result.findings:
            sev_color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(f.severity, "white")
            table.add_row(
                f"[{sev_color}]{f.severity}[/{sev_color}]",
                str(f.line),
                f.category,
                f.description,
                f"-{f.penalty}",
            )
        console.print(table)

    if result.error:
        console.print(f"[red]Error: {result.error}[/red]")


@skills_app.command(name="remove")
def skills_remove(name: str = typer.Argument(help="Skill name to uninstall")) -> None:
    """Uninstall a user skill."""
    import shutil
    from pathlib import Path as _P

    dest = _P.home() / ".pincer" / "skills" / name
    if not dest.exists():
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(1)
    shutil.rmtree(dest)
    console.print(f"[green]Skill '{name}' removed[/green]")


@skills_app.command(name="info")
def skills_info(name: str = typer.Argument(help="Skill name")) -> None:
    """Show skill details."""
    import json
    from pathlib import Path as _P

    for base in [_P("skills"), _P.home() / ".pincer" / "skills"]:
        skill_dir = base / name
        manifest_path = skill_dir / "manifest.json"
        if manifest_path.exists():
            m = json.loads(manifest_path.read_text())
            console.print(f"[bold]{m.get('name', name)}[/bold] v{m.get('version', '?')}")
            console.print(f"  Description: {m.get('description', '')}")
            console.print(f"  Author:      {m.get('author', 'unknown')}")
            console.print(f"  Permissions: {', '.join(m.get('permissions', [])) or 'none'}")
            console.print(f"  Env:         {', '.join(m.get('env_required', [])) or 'none'}")
            tools = m.get("tools", [])
            console.print(f"  Tools:       {len(tools)}")
            for t in tools:
                console.print(f"    - {t['name']}: {t.get('description', '')}")
            return
    console.print(f"[red]Skill not found: {name}[/red]")


# ── Audit subcommands ─────────────────────────

audit_app = typer.Typer(name="audit", help="View and export audit logs")
app.add_typer(audit_app, name="audit")


@audit_app.callback(invoke_without_command=True)
def audit_default(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", help="Number of entries"),
    action: str = typer.Option("", "--action", help="Filter by action type"),
    user: str = typer.Option("", "--user", help="Filter by user ID"),
    since: str = typer.Option("", "--since", help="Filter from date (ISO)"),
    export: str = typer.Option("", "--export", help="Export to JSON file"),
) -> None:
    """View audit log entries."""
    if ctx.invoked_subcommand is not None:
        return
    asyncio.run(_show_audit(limit=limit, action=action, user=user, since=since, export=export))


async def _show_audit(
    limit: int = 50,
    action: str = "",
    user: str = "",
    since: str = "",
    export: str = "",
) -> None:
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.security.audit import AuditAction, AuditLogger

    s = get_settings_relaxed()
    audit_db = s.data_dir / "audit.db"
    logger = AuditLogger(db_path=audit_db)
    await logger.initialize()

    if export:
        count = await logger.export_json(
            export,
            user_id=user or None,
            since=since or None,
        )
        console.print(f"[green]Exported {count} entries to {export}[/green]")
        await logger.shutdown()
        return

    action_filter = None
    if action:
        try:
            action_filter = AuditAction(action)
        except ValueError:
            console.print(f"[red]Invalid action: {action}[/red]")
            console.print(f"Valid actions: {', '.join(a.value for a in AuditAction)}")
            await logger.shutdown()
            return

    results = await logger.query(
        user_id=user or None,
        action=action_filter,
        since=since or None,
        limit=limit,
    )

    if not results:
        console.print("[dim]No audit entries found.[/dim]")
        await logger.shutdown()
        return

    table = Table(title=f"Audit Log (last {len(results)})")
    table.add_column("Time", width=20)
    table.add_column("User", width=12)
    table.add_column("Action", width=16)
    table.add_column("Tool", width=15)
    table.add_column("Summary")
    table.add_column("Cost", justify="right", width=10)

    for row in results:
        ts = (row.get("timestamp") or "")[:19]
        cost_str = f"${row.get('cost_usd', 0):.4f}" if row.get("cost_usd") else ""
        summary = (row.get("input_summary") or "")[:60]
        table.add_row(
            ts,
            str(row.get("user_id", ""))[:12],
            str(row.get("action", "")),
            str(row.get("tool", "") or ""),
            summary,
            cost_str,
        )

    console.print(table)

    stats = await logger.get_stats()
    console.print(
        f"\n  Total: {stats['total_entries']} entries  "
        f"Cost: ${stats['total_cost_usd']:.4f}  "
        f"Failed: {stats['failed_actions']}"
    )
    await logger.shutdown()


# ── Memory subcommands ────────────────────────

memory_app = typer.Typer(name="memory", help="Manage conversation memory")
app.add_typer(memory_app, name="memory")


@memory_app.command(name="search")
def memory_search(query: str = typer.Argument(help="Search query")) -> None:
    """Search conversation memory."""
    asyncio.run(_memory_search(query))


async def _memory_search(query: str) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.store import MemoryStore

    s = get_settings_relaxed()
    store = MemoryStore(s.db_path)
    await store.initialize()
    results = await store.search_text(query, limit=10)
    if not results:
        console.print("[dim]No memories found.[/dim]")
    else:
        for i, mem in enumerate(results, 1):
            console.print(f"  {i}. [{mem.category}] {mem.content[:200]}")
    await store.close()


@memory_app.command(name="stats")
def memory_stats() -> None:
    """Show memory usage statistics."""
    asyncio.run(_memory_stats())


async def _memory_stats() -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.store import MemoryStore

    s = get_settings_relaxed()
    store = MemoryStore(s.db_path)
    await store.initialize()

    async with store._db.execute("SELECT COUNT(*) FROM memories") as cur:  # type: ignore[union-attr]
        row = await cur.fetchone()
        total = row[0] if row else 0
    async with store._db.execute("SELECT COUNT(DISTINCT user_id) FROM memories") as cur:  # type: ignore[union-attr]
        row = await cur.fetchone()
        users = row[0] if row else 0
    async with store._db.execute(  # type: ignore[union-attr]
        "SELECT category, COUNT(*) FROM memories GROUP BY category"
    ) as cur:
        categories = {r[0]: r[1] async for r in cur}

    console.print("[bold]Memory Stats[/bold]")
    console.print(f"  Total memories: {total}")
    console.print(f"  Users:          {users}")
    for cat, count in categories.items():
        console.print(f"  {cat}: {count}")
    await store.close()


@memory_app.command(name="clear")
def memory_clear(
    user_id: str = typer.Option(..., "--user", help="User ID to clear"),
) -> None:
    """Clear memory for a user."""
    asyncio.run(_memory_clear(user_id))


async def _memory_clear(user_id: str) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.store import MemoryStore

    s = get_settings_relaxed()
    store = MemoryStore(s.db_path)
    await store.initialize()
    await store._db.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))  # type: ignore[union-attr]
    await store._db.commit()  # type: ignore[union-attr]
    console.print(f"[green]Cleared memories for {user_id}[/green]")
    await store.close()


@memory_app.command(name="export")
def memory_export(
    user_id: str = typer.Option(..., "--user", help="User ID to export"),
    output: str = typer.Option("memories.json", "--output", help="Output file"),
) -> None:
    """Export user memories to JSON."""
    asyncio.run(_memory_export(user_id, output))


async def _memory_export(user_id: str, output: str) -> None:
    import json as _json

    from pincer.config import get_settings_relaxed
    from pincer.memory.store import MemoryStore

    s = get_settings_relaxed()
    store = MemoryStore(s.db_path)
    await store.initialize()

    async with store._db.execute(  # type: ignore[union-attr]
        "SELECT content, category, created_at FROM memories WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ) as cur:
        records = [{"content": r[0], "category": r[1], "created_at": r[2]} async for r in cur]

    from pathlib import Path as _P

    _P(output).write_text(_json.dumps(records, indent=2))
    console.print(f"[green]Exported {len(records)} memories to {output}[/green]")
    await store.close()


# ── Schedule subcommands ──────────────────────

schedule_app = typer.Typer(name="schedule", help="Manage scheduled tasks")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command(name="list")
def schedule_list() -> None:
    """List all scheduled tasks."""
    asyncio.run(_schedule_list())


async def _schedule_list() -> None:
    import aiosqlite as _aiosqlite
    from rich.table import Table

    from pincer.config import get_settings_relaxed

    s = get_settings_relaxed()
    async with _aiosqlite.connect(str(s.db_path)) as db:
        try:
            async with db.execute(
                "SELECT name, cron_expr, pincer_user_id, timezone, enabled FROM schedules ORDER BY name"
            ) as cur:
                rows = [(r[0], r[1], r[2], r[3], r[4]) async for r in cur]
        except Exception:
            console.print("[dim]No scheduled tasks (table not created yet).[/dim]")
            return

    if not rows:
        console.print("[dim]No scheduled tasks.[/dim]")
        return

    table = Table(title="Scheduled Tasks")
    table.add_column("Name")
    table.add_column("Cron")
    table.add_column("User")
    table.add_column("Timezone")
    table.add_column("Enabled")

    for name, cron, user, tz, enabled in rows:
        table.add_row(name, cron, user or "", tz or "", "yes" if enabled else "no")
    console.print(table)


# ── MCP subcommands ───────────────────────────

mcp_app = typer.Typer(name="mcp", help="Manage MCP server connections and registry")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command(name="list")
def mcp_list() -> None:
    """List configured MCP servers and their status."""
    asyncio.run(_mcp_list())


async def _mcp_list() -> None:
    from rich.table import Table

    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    if not cfg.enabled:
        console.print("[dim]MCP disabled (PINCER_MCP_ENABLED=false)[/dim]")
        return
    if not cfg.servers:
        console.print("[dim]No MCP servers configured.[/dim]")
        console.print("  Add servers in pincer.toml [[mcp.servers]] or PINCER_MCP_SERVER_1_* env vars.")
        return

    table = Table(title="MCP Servers")
    table.add_column("Name", style="bold")
    table.add_column("Transport")
    table.add_column("Enabled")
    table.add_column("Sandbox")
    table.add_column("Approval")
    table.add_column("Timeout")

    for s in cfg.servers:
        table.add_row(
            s.name,
            s.transport.value,
            "[green]yes[/green]" if s.enabled else "[dim]no[/dim]",
            "[green]yes[/green]" if s.sandbox else "[yellow]no[/yellow]",
            ", ".join(s.approval_required),
            f"{s.timeout_seconds}s",
        )
    console.print(table)


@mcp_app.command(name="test")
def mcp_test(
    server: str = typer.Argument(..., help="Server name to test"),
) -> None:
    """Connect to an MCP server, list its tools, then disconnect (dry-run)."""
    asyncio.run(_mcp_test(server))


async def _mcp_test(server_name: str) -> None:
    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    srv = next((s for s in cfg.servers if s.name == server_name), None)
    if not srv:
        console.print(f"[red]Server '{server_name}' not found in config.[/red]")
        console.print(f"  Configured: {[s.name for s in cfg.servers]}")
        raise typer.Exit(1)

    console.print(f"[bold]Testing MCP server: {server_name}[/bold]")
    console.print(f"  Transport: {srv.transport.value}")
    console.print(f"  Command:   {srv.command or srv.url}")

    session = MCPClientSession(srv)
    try:
        with console.status("[bold green]Connecting...[/bold green]"):
            await session.connect()

        console.print(f"[green]Connected! {len(session.tools)} tools discovered.[/green]\n")

        if session.tools:
            table = Table(title=f"Tools from '{server_name}'")
            table.add_column("Tool Name", style="bold")
            table.add_column("Description")
            for tool in session.tools:
                desc = (getattr(tool, "description", None) or "")[:80]
                table.add_row(tool.name, desc)
            console.print(table)

    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        await session.disconnect()
        console.print("\n[dim]Disconnected.[/dim]")


@mcp_app.command(name="tools")
def mcp_tools(
    server: str = typer.Option("", "--server", help="Filter by server name"),
) -> None:
    """List all MCP tools (optionally filtered by server)."""
    asyncio.run(_mcp_tools(server))


async def _mcp_tools(server_filter: str) -> None:
    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return

    cfg = load_mcp_config()
    servers_to_check = [s for s in cfg.servers if s.enabled]
    if server_filter:
        servers_to_check = [s for s in servers_to_check if s.name == server_filter]
        if not servers_to_check:
            console.print(f"[red]Server '{server_filter}' not found.[/red]")
            raise typer.Exit(1)

    table = Table(title="MCP Tools")
    table.add_column("Server", style="bold")
    table.add_column("Tool")
    table.add_column("Description")
    table.add_column("Approval")

    for srv in servers_to_check:
        session = MCPClientSession(srv)
        try:
            await session.connect()
            from pincer.mcp.bridge import _requires_approval

            for tool in session.tools:
                approval = _requires_approval(tool, srv.approval_required)
                desc = (getattr(tool, "description", None) or "")[:60]
                table.add_row(
                    srv.name,
                    tool.name,
                    desc,
                    "[yellow]required[/yellow]" if approval else "[dim]none[/dim]",
                )
        except Exception as e:
            table.add_row(srv.name, "(failed)", str(e)[:60], "")
        finally:
            await session.disconnect()

    console.print(table)


@mcp_app.command(name="call")
def mcp_call(
    server: str = typer.Argument(..., help="Server name"),
    tool: str = typer.Argument(..., help="Tool name"),
    args: list[str] = typer.Option([], "--arg", help="key=value argument (repeatable)"),  # noqa: B008
) -> None:
    """Manually call an MCP tool for debugging (e.g. --arg key=value)."""
    asyncio.run(_mcp_call(server, tool, args))


async def _mcp_call(server_name: str, tool_name: str, raw_args: list[str]) -> None:
    import json as _json

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return

    cfg = load_mcp_config()
    srv = next((s for s in cfg.servers if s.name == server_name), None)
    if not srv:
        console.print(f"[red]Server '{server_name}' not found.[/red]")
        raise typer.Exit(1)

    # Parse --arg key=value
    arguments: dict = {}
    for raw in raw_args:
        if "=" not in raw:
            console.print(f"[red]Invalid argument format '{raw}' — use key=value[/red]")
            raise typer.Exit(1)
        k, v = raw.split("=", 1)
        # Try to parse JSON values (numbers, booleans, etc.)
        try:
            arguments[k.strip()] = _json.loads(v)
        except _json.JSONDecodeError:
            arguments[k.strip()] = v

    session = MCPClientSession(srv)
    try:
        await session.connect()
        console.print(f"[bold]Calling {server_name}.{tool_name}[/bold]")
        console.print(f"  Arguments: {arguments}")
        result = await session.call_tool(tool_name, arguments)
        from pincer.mcp.bridge import _format_result

        output = _format_result(result)
        console.print("\n[bold]Result:[/bold]")
        console.print(output)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        await session.disconnect()


@mcp_app.command(name="status")
def mcp_status() -> None:
    """Show live connectivity status for all configured MCP servers."""
    asyncio.run(_mcp_status())


async def _mcp_status() -> None:
    import contextlib
    import time

    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    if not cfg.enabled:
        console.print("[dim]MCP disabled (PINCER_MCP_ENABLED=false)[/dim]")
        return
    if not cfg.servers:
        console.print("[dim]No MCP servers configured.[/dim]")
        return

    table = Table(title="MCP Server Status")
    table.add_column("Name", style="bold")
    table.add_column("Transport")
    table.add_column("Connected")
    table.add_column("Tools")
    table.add_column("Latency")

    for srv in cfg.servers:
        if not srv.enabled:
            table.add_row(srv.name, srv.transport.value, "[dim]disabled[/dim]", "-", "-")
            continue

        session = MCPClientSession(srv)
        latency_ms = "-"
        tool_count = "-"
        connected_cell = "[red]no[/red]"

        try:
            t0 = time.monotonic()
            await session.connect()
            latency_ms = f"{(time.monotonic() - t0) * 1000:.0f}ms"
            tool_count = str(len(session.tools))
            connected_cell = "[green]yes[/green]"
        except Exception as e:
            connected_cell = f"[red]no[/red] ({e})"
        finally:
            with contextlib.suppress(Exception):
                await session.disconnect()

        table.add_row(srv.name, srv.transport.value, connected_cell, tool_count, latency_ms)

    console.print(table)


# ── MCP server subcommands ─────────────────────────────────────────────────

mcp_server_app = typer.Typer(name="server", help="Manage the Pincer MCP export server")
mcp_app.add_typer(mcp_server_app, name="server")


@mcp_server_app.command(name="status")
def mcp_server_status_cmd() -> None:
    """Show whether the Pincer MCP export server is enabled and its config."""
    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return
    cfg = load_mcp_config()
    srv = cfg.server
    if not srv.enabled:
        console.print("[dim]MCP server export is disabled.[/dim]")
        console.print("  Enable with: [bold]PINCER_MCP_SERVER_EXPORT_ENABLED=true[/bold]")
        console.print("  Or set [mcp.server] enabled = true in pincer.toml")
        return
    console.print("[green]MCP server export ENABLED[/green]")
    console.print(f"  Endpoint:      {srv.host}:{srv.port}{srv.path}")
    console.print(f"  Exposed tools: {', '.join(srv.expose_tools) or '(none)'}")
    console.print("  + pincer_ask_user (always)")


@mcp_server_app.command(name="config")
def mcp_server_config_cmd() -> None:
    """Print MCP client config JSON for Claude Desktop / Cursor."""
    import json

    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return
    cfg = load_mcp_config()
    srv = cfg.server
    url = f"http://{srv.host}:{srv.port}{srv.path}"
    client_cfg = {
        "mcpServers": {
            "pincer": {
                "type": "streamable-http",
                "url": url,
            }
        }
    }
    console.print(json.dumps(client_cfg, indent=2))
    console.print(
        "\n[dim]Add the above to Claude Desktop's MCP config, then start Pincer with MCP server enabled.[/dim]"
    )


# ── pincer mcp search ─────────────────────────────────────────────────────────


@mcp_app.command(name="search")
def mcp_search(
    query: str = typer.Argument(..., help="Search query"),
    registry: str = typer.Option("all", "--registry", "-r", help="Registry to search: mcp|clawhub|all"),
) -> None:
    """Search MCP Registry and ClawHub for available MCP servers."""
    asyncio.run(_mcp_search(query, registry))


async def _mcp_search(query: str, registry: str) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    console.print(f"[dim]Searching '{registry}' registry for '{query}'...[/dim]")
    client = MCPRegistryClient()
    try:
        results = await client.search(query, registry=registry)
    except Exception as e:
        console.print(f"[red]Search failed: {e}[/red]")
        raise typer.Exit(1) from e

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    from rich.table import Table

    table = Table(title=f"MCP servers matching '{query}'", show_lines=False)
    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Name", style="green")
    table.add_column("Registry", style="dim")
    table.add_column("Description")
    table.add_column("Downloads", justify="right", style="dim")

    for entry in results:
        table.add_row(
            entry.package_name,
            entry.name,
            entry.registry,
            entry.description[:80] if entry.description else "",
            str(entry.downloads) if entry.downloads else "",
        )

    console.print(table)
    console.print(f"\n[dim]Found {len(results)} result(s). Install with:[/dim] pincer mcp install <package>")


# ── pincer mcp install ────────────────────────────────────────────────────────


@mcp_app.command(name="install")
def mcp_install(
    package: str = typer.Argument(..., help="Package name, e.g. @modelcontextprotocol/server-github"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip security scan (not recommended)"),
    name: str | None = typer.Option(None, "--name", "-n", help="Override config server name"),
) -> None:
    """Install an MCP server: download, scan, and add to pincer.toml."""
    asyncio.run(_mcp_install(package, scan=not no_scan, name_override=name))


async def _mcp_install(package: str, scan: bool, name_override: str | None) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    client = MCPRegistryClient()
    console.print(f"[dim]Installing '{package}'...[/dim]")

    if not scan:
        console.print("[yellow]⚠ Security scan disabled — install at your own risk[/yellow]")

    try:
        config, scan_info = await client.install(package, scan=scan, name_override=name_override)
    except ValueError as e:
        # Scan blocked
        console.print(f"[red]✗ Install blocked by security scan: {e}[/red]")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Install failed: {e}[/red]")
        raise typer.Exit(1) from e

    # Display scan result
    if not scan_info.get("skipped"):
        score = scan_info["score"]
        summary = scan_info.get("summary", f"Score {score}/100")
        color = "green" if score >= 80 else "yellow" if score >= 40 else "red"
        console.print(f"[{color}]Security scan: {summary}[/{color}]")
        if score < 80 and not typer.confirm("Score below 80. Install anyway?", default=False):
            console.print("[yellow]Installation cancelled.[/yellow]")
            raise typer.Exit(0)

    # Append to pincer.toml
    from pathlib import Path as _InstPath

    toml_path = _InstPath.cwd() / "pincer.toml"
    _append_server_to_toml(toml_path, config)

    console.print(f"[green]✓ Installed '{config.name}'[/green]")
    console.print(f"  Command: {config.command} {' '.join(config.args)}")
    if config.env:
        console.print(f"  Required env vars: {', '.join(config.env.keys())}")
    console.print(f"\n[dim]Config written to {toml_path}. Run 'pincer run' to connect.[/dim]")


def _append_server_to_toml(toml_path: object, config: object) -> None:
    """Append a new [[mcp.servers]] entry to pincer.toml."""
    existing = toml_path.read_text() if toml_path.exists() else ""
    lines: list[str] = []

    # Ensure [mcp] section exists
    if "[mcp]" not in existing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += "\n[mcp]\nenabled = true\n"

    # Build the new server entry
    lines.append("\n[[mcp.servers]]")
    lines.append(f'name = "{config.name}"')
    lines.append(f'transport = "{config.transport.value}"')
    if config.command:
        lines.append(f'command = "{config.command}"')
    if config.args:
        args_toml = "[" + ", ".join(f'"{a}"' for a in config.args) + "]"
        lines.append(f"args = {args_toml}")
    if config.env:
        lines.append("[mcp.servers.env]")
        for k, v in config.env.items():
            lines.append(f'{k} = "{v}"')
    lines.append('approval_required = ["*"]')

    toml_path.write_text(existing + "\n".join(lines) + "\n")


# ── pincer mcp scan ───────────────────────────────────────────────────────────


@mcp_app.command(name="scan")
def mcp_scan(
    path_or_package: str = typer.Argument(..., help="Path to directory or package name to scan"),
) -> None:
    """Run an AST security scan on a local directory or installed package."""
    from pathlib import Path as _Path

    target = _Path(path_or_package)
    if target.exists():
        _scan_local(target)
    else:
        asyncio.run(_scan_package_name(path_or_package))


def _scan_local(path: object) -> None:
    try:
        from pincer.skills.scanner import PackageScanner
    except ImportError:
        console.print("[red]Scanner not available[/red]")
        raise typer.Exit(1) from None

    console.print(f"[dim]Scanning {path}...[/dim]")
    scanner = PackageScanner()
    result = scanner.scan_path(path)
    _print_scan_result(result)


async def _scan_package_name(package: str) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient, _infer_entry
    except ImportError:
        console.print("[red]Scanner not available[/red]")
        raise typer.Exit(1) from None

    entry = _infer_entry(package)
    client = MCPRegistryClient()
    console.print(f"[dim]Downloading and scanning '{package}'...[/dim]")
    try:
        result = await client._scan_package(entry)  # noqa: SLF001
    except Exception as e:
        console.print(f"[red]Scan failed: {e}[/red]")
        raise typer.Exit(1) from e
    _print_scan_result(result)


def _print_scan_result(result: object) -> None:
    score = result.score
    color = "green" if score >= 80 else "yellow" if score >= 40 else "red"
    icon = "✓" if score >= 80 else "⚠" if score >= 40 else "✗"
    console.print(f"\n[{color}]{icon} {result.summary()}[/{color}]")
    console.print(f"  Files scanned: {result.files_scanned}")

    if result.findings:
        from rich.table import Table

        table = Table(show_header=True, show_lines=False)
        table.add_column("Risk", style="bold", width=8)
        table.add_column("Rule", width=20)
        table.add_column("Message")
        table.add_column("File", style="dim")
        table.add_column("Line", justify="right", style="dim", width=5)

        risk_colors = {"critical": "red", "high": "yellow", "medium": "blue", "info": "dim"}
        for f in result.findings:
            c = risk_colors.get(str(f.risk), "white")
            table.add_row(f"[{c}]{f.risk}[/{c}]", f.rule, f.message, f.file, str(f.line) if f.line else "")
        console.print(table)

    if result.blocked:
        console.print("[red]⛔ BLOCKED — score below 40, install not recommended[/red]")
    elif score < 80:
        console.print("[yellow]⚠ Score below 80 — review findings before installing[/yellow]")
    else:
        console.print("[green]✓ Safe to install[/green]")


# ── pincer mcp uninstall ──────────────────────────────────────────────────────


@mcp_app.command(name="uninstall")
def mcp_uninstall(
    server_name: str = typer.Argument(..., help="Server name as configured in pincer.toml"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove an MCP server from pincer.toml and clean up staging files."""
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    if not yes and not typer.confirm(f"Remove MCP server '{server_name}' from config?"):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(0)

    client = MCPRegistryClient()
    removed = client.uninstall(server_name)
    if removed:
        console.print(f"[green]✓ Removed '{server_name}' from pincer.toml[/green]")
    else:
        console.print(f"[yellow]Server '{server_name}' not found in pincer.toml[/yellow]")
        raise typer.Exit(1)


# ── pincer mcp serve ──────────────────────────────────────────────────────────


@mcp_app.command(name="serve")
def mcp_serve(
    host: str = typer.Option("", "--host", "-H", help="Override server host (default from config)"),
    port: int = typer.Option(0, "--port", "-p", help="Override server port (default from config)"),
    approval: str = typer.Option(
        "policy",
        "--approval",
        "-a",
        help="Approval mode: policy | cli | webhook",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start a standalone Pincer MCP server (no full agent required).

    External MCP clients (Claude Desktop, Cursor, VS Code) connect to this server
    and call Pincer tools. Approval is handled via the selected --approval mode:

    \b
      policy   Auto-approve/deny based on [mcp.server.approval_policy] config
      cli      Interactive terminal prompt (foreground, interactive)
      webhook  POST to mcp.server.webhook_url for external approval
    """
    asyncio.run(_mcp_serve(host=host, port=port, approval_mode=approval, verbose=verbose))


async def _mcp_serve(
    host: str,
    port: int,
    approval_mode: str,
    verbose: bool,
) -> None:
    import logging as _logging

    if verbose:
        _logging.basicConfig(level=_logging.DEBUG)

    try:
        from pincer.mcp.config import load_mcp_config
        from pincer.mcp.standalone import StandaloneMCPShell
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        raise typer.Exit(1) from None

    cfg = load_mcp_config()

    # Apply CLI host/port overrides
    if host or port:
        from dataclasses import replace as _replace

        srv = cfg.server
        srv_overridden = _replace(
            srv,
            host=host or srv.host,
            port=port or srv.port,
        )
        from dataclasses import replace as _r

        cfg = _r(cfg, server=srv_overridden)

    if not cfg.server.enabled:
        console.print(
            "[yellow]MCP server export is not enabled.[/yellow]\n"
            "  Set [bold]enabled = true[/bold] under [mcp.server] in pincer.toml, or:\n"
            "  [bold]PINCER_MCP_SERVER_EXPORT_ENABLED=true[/bold]"
        )
        raise typer.Exit(1)

    if approval_mode not in ("policy", "cli", "webhook"):
        console.print(f"[red]Unknown approval mode: '{approval_mode}'. Choose: policy | cli | webhook[/red]")
        raise typer.Exit(1)

    # Build approval policy from config
    policy_cfg = cfg.server.approval_policy
    approval_policy = policy_cfg.as_dict() if policy_cfg else {"default": "deny"}

    webhook_url = getattr(cfg.server, "webhook_url", None)
    if approval_mode == "webhook" and not webhook_url:
        console.print("[red]approval_mode='webhook' requires mcp.server.webhook_url in pincer.toml[/red]")
        raise typer.Exit(1)

    console.print("[bold]Starting Pincer MCP server[/bold]")
    console.print(f"  Endpoint:  http://{cfg.server.host}:{cfg.server.port}{cfg.server.path}")
    console.print(f"  Approval:  {approval_mode}")
    console.print(f"  Tools:     {', '.join(cfg.server.expose_tools) or '(none)'}")
    if cfg.servers:
        console.print(f"  Clients:   {len(cfg.servers)} external MCP server(s)")

    shell = StandaloneMCPShell(
        mcp_config=cfg,
        approval_mode=approval_mode,
        webhook_url=webhook_url,
        approval_policy=approval_policy,
    )

    try:
        await shell.run()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# ═══════════════════════════════════════════════
# Google Workspace setup command
# ═══════════════════════════════════════════════


@app.command(name="setup-google")
def setup_google() -> None:
    """One-time Google Workspace OAuth setup (opens browser for consent)."""
    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.auth import ALL_SCOPES, GoogleAuth

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_workspace_token.json"

    console.print("[bold]Google Workspace Setup[/bold]\n")
    console.print("This sets up all Google Workspace tools: Gmail, Calendar, Drive,")
    console.print("Docs, Sheets, Slides, Tasks, and Contacts (85 tools total).\n")

    if not credentials_path.exists():
        console.print(f"[red]Missing: {credentials_path}[/red]\n")
        console.print("Complete these steps in Google Cloud Console (~5 minutes):\n")
        console.print("  1. Go to [link]https://console.cloud.google.com/apis/credentials[/link]")
        console.print("  2. Create or select a project")
        console.print("  3. Enable APIs & Services → Library:")
        console.print("     Gmail API, Google Calendar API, Google Drive API,")
        console.print("     Google Docs API, Google Sheets API, Google Slides API,")
        console.print("     Google Tasks API, People API")
        console.print("  4. OAuth consent screen → External → Test mode → add your email")
        console.print("  5. Credentials → Create → OAuth 2.0 Client ID → Desktop App")
        console.print(f"  6. Download JSON → save as:\n     {credentials_path}\n")
        raise typer.Exit(1)

    if token_path.exists():
        console.print(f"[yellow]Token already exists: {token_path}[/yellow]")
        if not typer.confirm("Re-authenticate (overwrites existing token)?"):
            raise typer.Exit(0)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        console.print("[red]google-auth-oauthlib not installed.[/red]\nRun:  uv pip install google-auth-oauthlib")
        raise typer.Exit(1) from None

    console.print(f"Requesting {len(ALL_SCOPES)} scope(s) for all Workspace APIs...")
    console.print("Opening browser for Google consent...\n")

    auth = GoogleAuth(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
    )
    try:
        creds = auth.run_auth_flow(open_browser=True)
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    console.print("\n[green]Google Workspace authenticated![/green]")
    console.print(f"  Token saved to: {token_path}")
    console.print(f"  Refresh token:  {'Yes' if creds.refresh_token else 'No'}")
    console.print(f"  Scopes granted: {len(ALL_SCOPES)}")
    console.print("\n85 Google Workspace tools are now available in Pincer.")
    console.print("Start the agent:  [bold]pincer run[/bold]")


@app.command(name="setup-ms365")
def setup_ms365() -> None:
    """One-time Microsoft 365 setup (device code auth flow)."""
    import asyncio

    console.print("[bold]Microsoft 365 Setup[/bold]\n")
    console.print("This sets up all Microsoft 365 tools: Outlook email, Calendar, OneDrive,")
    console.print("To Do, Teams, Contacts, and OneNote (69 tools total).\n")

    try:
        import msal  # noqa: F401
    except ImportError:
        console.print("[red]msal not installed.[/red]\nRun:  uv pip install msal")
        raise typer.Exit(1) from None

    console.print("[bold]Step 1: Azure App Registration[/bold]\n")
    console.print("If you haven't registered an app yet:\n")
    console.print("  1. Go to https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")
    console.print("  2. Click 'New registration'")
    console.print("  3. Name: 'Pincer Agent'")
    console.print("  4. Supported account types: choose based on your account type:")
    console.print("     - 'Personal Microsoft accounts only' for personal Outlook/OneDrive")
    console.print("     - 'Accounts in any org directory + personal' for work + personal")
    console.print("  5. Redirect URI: leave empty (we use device code flow)")
    console.print("  6. Click 'Register'")
    console.print("  7. Under 'Authentication', enable 'Allow public client flows'")
    console.print("  8. Copy the Application (client) ID\n")

    client_id = typer.prompt("Paste your Application (client) ID")
    if not client_id or len(client_id) < 10:
        console.print("[red]Invalid client ID.[/red]")
        raise typer.Exit(1)

    tenant_id = typer.prompt("Tenant ID (press Enter for 'common')", default="common")

    from pincer.integrations.ms365.auth import MS365Auth
    from pincer.integrations.ms365.config import MS365IntegrationConfig, resolve_cache_path

    cfg = MS365IntegrationConfig(client_id=client_id, tenant_id=tenant_id)
    cache_path = resolve_cache_path(cfg)

    if cache_path.exists():
        console.print(f"[yellow]Token cache already exists: {cache_path}[/yellow]")
        if not typer.confirm("Re-authenticate (overwrites existing token)?"):
            raise typer.Exit(0)

    auth = MS365Auth(
        client_id=client_id,
        tenant_id=tenant_id,
        cache_path=str(cache_path),
    )

    console.print(f"\nRequesting {len(auth.scopes)} permission scope(s)...")
    console.print("Starting device code authentication...\n")

    try:
        result = asyncio.run(auth.device_code_flow())
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    # Show the device code message (it was set during the flow)
    if auth._pending_flow_message:
        console.print(f"[bold cyan]{auth._pending_flow_message}[/bold cyan]\n")

    account = auth.authenticated_account() or result.get("id_token_claims", {}).get("preferred_username", "unknown")
    console.print("\n[green]Microsoft 365 authenticated![/green]")
    console.print(f"  Account:      {account}")
    console.print(f"  Token cached: {cache_path}")
    console.print(f"  Scopes:       {len(auth.scopes)}")

    # Save config to pincer.toml
    _save_ms365_config(client_id, tenant_id)

    console.print("\n69 Microsoft 365 tools are now available in Pincer.")
    console.print("Start the agent:  [bold]pincer run[/bold]")


def _save_ms365_config(client_id: str, tenant_id: str) -> None:
    """Append MS365 config to pincer.toml if not already present."""
    from pathlib import Path

    toml_path = Path.cwd() / "pincer.toml"

    if toml_path.exists():
        content = toml_path.read_text()
        if "[integrations.ms365]" in content:
            return  # already configured
    else:
        content = ""

    section = f'\n[integrations.ms365]\nenabled = true\nclient_id = "{client_id}"\ntenant_id = "{tenant_id}"\n'
    toml_path.write_text(content + section)
    console.print(f"  Config saved:  {toml_path}")


# ═══════════════════════════════════════════════
# Slack setup command
# ═══════════════════════════════════════════════


@app.command(name="setup-slack")
def setup_slack() -> None:
    """Interactive Slack bot token setup (71 tools: channels, messages, files, and more)."""
    import asyncio

    from pincer.integrations.slack.auth import save_tokens, validate_bot_token

    console.print("[bold]Slack Setup[/bold]\n")
    console.print("This configures all 71 Slack tools: channels, messages, threads,")
    console.print("files, reactions, pins, bookmarks, reminders, search, and user management.\n")

    console.print("Steps to create a Slack App (~5 minutes):\n")
    console.print("  1. Go to [link]https://api.slack.com/apps[/link] → Create New App → From Scratch")
    console.print("  2. Name: 'Pincer Agent', select your workspace")
    console.print("  3. OAuth & Permissions → Bot Token Scopes → add these scopes:")
    console.print("     channels:read, channels:write, channels:history, channels:join,")
    console.print("     chat:write, files:read, files:write, groups:read, groups:write,")
    console.print("     groups:history, im:read, im:write, im:history, mpim:read, mpim:write,")
    console.print("     mpim:history, pins:read, pins:write, reactions:read, reactions:write,")
    console.print("     reminders:read, reminders:write, users:read, users:read.email,")
    console.print("     usergroups:read, usergroups:write, bookmarks:read, bookmarks:write,")
    console.print("     emoji:read, dnd:read")
    console.print("  4. Install to Workspace → Authorize")
    console.print("  5. Copy Bot Token (xoxb-...)")
    console.print("  6. Optional: User Token Scopes → search:read, then copy User Token (xoxp-...)")
    console.print("     (required for slack__search_messages and slack__search_files)\n")

    bot_token = typer.prompt("Paste Bot Token (xoxb-...)", hide_input=True)
    bot_token = bot_token.strip()
    if not bot_token.startswith("xoxb-"):
        console.print("[red]Error: bot token must start with 'xoxb-'[/red]")
        raise typer.Exit(1)

    console.print("\nValidating bot token...")
    try:
        info = asyncio.run(validate_bot_token(bot_token))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Authenticated![/green]  workspace: {info['workspace']}, bot: {info['bot_user']}")

    user_token = ""
    if typer.confirm("\nDo you have a User Token (xoxp-...) for search features?", default=False):
        user_token = typer.prompt("Paste User Token (xoxp-...)", hide_input=True).strip()
        if user_token and not user_token.startswith("xoxp-"):
            console.print("[yellow]Warning: user token should start with 'xoxp-' — saving anyway.[/yellow]")

    token_path = save_tokens(bot_token, user_token)
    console.print(f"\n[green]Tokens saved to:[/green] {token_path}")
    console.print("\n[bold]71 Slack tools are now available in Pincer.[/bold]")

    # ── Slack Channel (Socket Mode) ──────────────────────────────────────────
    console.print("\n[bold]Slack Channel Setup (optional — lets users DM Pincer)[/bold]")
    console.print("To enable the Slack channel, you need an App-Level Token (xapp-...).")
    console.print("Steps:\n")
    console.print("  1. Settings → Socket Mode → Enable Socket Mode")
    console.print("  2. Generate an App-Level Token with scope: [bold]connections:write[/bold]")
    console.print("  3. Copy the token (starts with xapp-)")
    console.print("  4. Subscribe to bot events (Event Subscriptions → Bot Events):")
    console.print("       app_mention, message.channels, message.groups,")
    console.print("       message.im, message.mpim")
    console.print("  5. Re-install to workspace if prompted\n")

    if typer.confirm("Do you want to configure the Slack channel now?", default=False):
        app_token = typer.prompt("Paste App-Level Token (xapp-...)", hide_input=True).strip()
        if not app_token.startswith("xapp-"):
            console.print("[yellow]Warning: app token should start with 'xapp-' — saving anyway.[/yellow]")

        # Persist tokens in .env (create or update)
        env_path = _find_env_file()
        _upsert_env(env_path, "PINCER_SLACK_BOT_TOKEN", bot_token)
        _upsert_env(env_path, "PINCER_SLACK_APP_TOKEN", app_token)
        console.print(f"[green]Channel tokens saved to:[/green] {env_path}")
        console.print("\n[bold green]Slack channel ready![/bold green]")
        console.print("Users can now DM @Pincer or @mention it in channels.")

    console.print("\nStart the agent:  [bold]pincer run[/bold]")
