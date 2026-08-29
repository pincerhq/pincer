"""
Pincer CLI — the main entry point.

Usage:
    pincer run          Start the agent
    pincer run tasks    Start only the background task worker (requires PINCER_TASK_BROKER=redis)
    pincer config       Show current configuration
    pincer cost         Show today's spend
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    from pincer.channels.base import BaseChannel, IncomingMessage
    from pincer.channels.microsoft_teams import MicrosoftTeamsChannel
    from pincer.config import Settings
    from pincer.core.agent import Agent
    from pincer.core.session import SessionManager
    from pincer.llm.base import BaseLLMProvider
    from pincer.llm.cost_tracker import CostTracker
    from pincer.llm.router import LLMRouter
    from pincer.mcp import MCPClientManager
    from pincer.memory.base import BaseMemoryBackend
    from pincer.memory.summarizer import Summarizer
    from pincer.security.audit import AuditLogger
    from pincer.security.rate_limiter import RateLimiter
    from pincer.tools.registry import ToolRegistry
    from pincer.tools.skills.index import SkillIndex

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
        # NOTE: rich formatting temporarily disabled
        # handlers=[RichHandler(console=console, show_path=False, markup=True)],
    )
    # format="%(message)s",

    # T8.5: no raw E.164 number reaches any log sink. Installed on the root
    # handlers right after basicConfig so it covers every module's logger,
    # including third-party ones (twilio, httpx) that echo call metadata.
    from pincer.voice.pii_guard import install_log_pii_filter

    install_log_pii_filter()


async def _ensure_ops_schedules(scheduler: Any, settings: Any) -> None:
    """Create the Sprint 9 ops schedules if they don't exist yet.

    Idempotent by schedule name. Each job is gated on its own prerequisite:
    the alert scanner needs somewhere to deliver, the canary needs a target
    number, and the digest needs a recipient.
    """
    ops_user = settings.ops_user_id or settings.default_user_id or ""
    tz = settings.voice_timezone or settings.timezone
    if not ops_user:
        return

    try:
        existing = {s["name"] for s in await scheduler.list_schedules(ops_user)}
    except Exception as e:
        console.print(f"[yellow]Ops schedule lookup failed: {e}[/yellow]")
        return

    jobs: list[tuple[str, str, dict[str, Any], bool, str]] = [
        (
            "ops_alert_scan",
            f"*/{max(1, int(settings.ops_alert_scan_interval_min))} * * * *",
            {"type": "ops_alert_scan"},
            bool(settings.ops_alerts_enabled),
            f"Ops alert scan every {settings.ops_alert_scan_interval_min}min",
        ),
        (
            "voice_canary",
            settings.voice_canary_cron,
            {"type": "voice_canary"},
            bool(settings.voice_canary_enabled and settings.voice_canary_number),
            f"Voice canary scheduled ({settings.voice_canary_cron})",
        ),
        (
            "voice_weekly_digest",
            settings.ops_digest_cron,
            {"type": "voice_weekly_digest"},
            bool(settings.ops_alerts_enabled),
            f"Weekly failure digest scheduled ({settings.ops_digest_cron})",
        ),
    ]

    for name, cron_expr, action, enabled, message in jobs:
        if not enabled or name in existing:
            continue
        try:
            await scheduler.add(
                name=name,
                cron_expr=cron_expr,
                action=action,
                pincer_user_id=ops_user,
                tz=tz,
                channel=settings.ops_channel or "telegram",
            )
            console.print(f"[green]{message}[/green]")
        except Exception as e:
            console.print(f"[yellow]Could not schedule {name}: {e}[/yellow]")


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
    """Return True if something is already listening on the given host:port.

    Defensive: unresolvable inputs (e.g. mocked settings in tests) count as
    "not in use" rather than crashing startup.
    """
    try:
        check_host = "127.0.0.1" if host == "0.0.0.0" else host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((check_host, int(port))) == 0
    except (TypeError, ValueError, OSError):
        return False


def _print_voice_webhook_urls(settings: Any, console: Any) -> None:  # type: ignore[no-untyped-def]
    base = (settings.voice_webhook_base_url or "").rstrip("/")
    if not base:
        return
    lines = [
        "[bold]Voice webhook URLs (configure in Twilio Console):[/bold]",
        f"  Inbound:           {base}/api/apps/twilio/webhook",
        f"  Status callback:   {base}/api/apps/twilio/status",
        f"  Fallback:          {base}/api/apps/twilio/fallback",
    ]
    engine = getattr(settings, "voice_engine", "conversation_relay").lower().strip()
    host = base
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if engine == "media_streams":
        lines.append(f"  Media stream WS:   wss://{host}/api/apps/twilio/stream/{{CallSid}}")
    else:
        lines.append(f"  ConversationRelay: wss://{host}/api/apps/twilio/relay")
    for line in lines:
        console.print(line)


class RunComponent(StrEnum):
    """A single component of the full agent that `pincer run` can start standalone."""

    TASKS = "tasks"


@app.command()
def run(
    component: RunComponent | None = typer.Argument(  # noqa: B008
        None,
        help=(
            "Run a single component standalone instead of the full agent. "
            "'tasks': background task worker only (requires PINCER_TASK_BROKER=redis)."
        ),
    ),
) -> None:
    """Start the Pincer agent, or a single component of it (see COMPONENT)."""
    from pincer.config import get_settings

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    _setup_logging(settings.log_level.value)

    if settings.telemetry_dsn:
        try:
            from pincer_telemetry import init as _init_telemetry

            import pincer as _pincer_pkg

            _init_telemetry(
                project_name=settings.agent_name,
                version=_pincer_pkg.__version__,
                dsn_url=str(settings.telemetry_dsn),
            )
            console.print("[green]Telemetry enabled[/green]")
        except ImportError:
            console.print(
                "[yellow]PINCER_TELEMETRY_DSN is set but opentelemetry packages are not installed — "
                'skipping. Install with: pip install "pincer-agent[telemetry]"[/yellow]'
            )
        except Exception as _tel_err:
            console.print(f"[yellow]Telemetry init failed (non-fatal): {_tel_err}[/yellow]")

    if component is RunComponent.TASKS:
        if settings.task_broker != "redis":
            console.print(
                "[red]pincer run tasks requires PINCER_TASK_BROKER=redis[/red] "
                "(the in-memory broker cannot be shared across processes)."
            )
            raise typer.Exit(1)
        asyncio.run(_run_tasks_worker(settings))
        return

    console.print(f"[bold green]{settings.agent_name} starting...[/bold green]")
    console.print(f"   Provider: {settings.default_provider}")
    console.print(f"   Model: {settings.default_model}")
    console.print(f"   Budget: ${settings.daily_budget_usd:.2f}/day")
    console.print(f"   Data: {settings.data_dir}")
    console.print(f"   Skills dir: {settings.skills_dir}")
    console.print()

    asyncio.run(_run_agent(settings))


def _format_pdf_attachment(pages: list[str], filename: str, abs_path: str, max_chars: int = 30_000) -> str:
    """Format a PDF attachment's extracted text for the LLM prompt.

    A scanned/image-only PDF has no embedded text layer, so pymupdf's
    `page.get_text()` returns empty/near-empty strings per page — detect that
    case (average non-whitespace chars/page below a conservative threshold;
    real text pages have hundreds, so this only misfires if *every* page in
    the document is near-blank) and say so explicitly instead of silently
    reporting a blank code block, so the agent knows to reach for an
    OCR-capable tool rather than guessing at the contents from the filename.
    """
    content = "\n\n".join(pages)
    non_ws_chars = len("".join(content.split()))
    is_scanned = bool(pages) and non_ws_chars < 20 * len(pages)
    if is_scanned:
        return (
            f"[File: {filename} — {len(pages)} pages] saved to {abs_path}.\n"
            "No embedded text found; this PDF appears to be a scanned/image-only "
            "document (pages are images, not text). If the user wants its text "
            f"read/extracted, call the OCR tool with file_path='{abs_path}' — "
            "do not guess at the contents from the filename, and do not try to "
            "transcribe it yourself."
        )
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... [truncated, {len(pages)} pages total]"
    return f"[File: {filename} — {len(pages)} pages, saved to {abs_path}]\n```\n{content}\n```"


_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _format_image_attachment(filename: str, abs_path: str, size_bytes: int, media_type: str) -> str:
    """Format an image attachment note for the LLM prompt.

    Images have no text layer the model can inspect ahead of time, and unlike
    a vision content block, an OCR tool needs a concrete argument (a file
    path here) to act on — the model cannot regenerate the original bytes
    from having merely "seen" the image. Give it that path directly, and
    tell it not to transcribe the image itself via vision, which is slow
    and produces nothing an OCR tool call can use.
    """
    return (
        f"[Image: {filename}] saved to {abs_path} ({size_bytes} bytes, {media_type}).\n"
        "If the user wants text read/extracted from this image, call the OCR tool "
        f"with file_path='{abs_path}' — do not try to transcribe it yourself."
    )


def _create_memory_backend(settings: Settings):  # type: ignore[return]
    """Factory: select and construct the configured memory backend."""
    if settings.memory_backend == "mcp":
        from pincer.memory.mcp import MCPMemoryBackend

        return MCPMemoryBackend(server_name=settings.memory_mcp_server)
    from pincer.memory.sqlite import SQLiteMemoryBackend

    return SQLiteMemoryBackend(settings.db_path)


@dataclass
class CoreComponents:
    """LLM/memory/tools/agent core shared by the full agent process and the standalone tasks worker.

    Built by `_build_core`; excludes channel-bound tools (send_file/send_image,
    registered separately by `_register_channel_bound_tools`) since those need
    a channel_map that only exists where channels are actually started.
    """

    session_mgr: SessionManager
    cost_tracker: CostTracker
    audit_logger: AuditLogger | None
    rate_limiter: RateLimiter
    llm_router: LLMRouter
    llm: BaseLLMProvider
    memory_store: BaseMemoryBackend | None
    summarizer: Summarizer | None
    tools: ToolRegistry
    skill_index: SkillIndex
    mcp_manager: MCPClientManager | None
    agent: Agent


async def _build_core(settings: Settings) -> CoreComponents:
    from pincer.core.agent import Agent
    from pincer.core.session import SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.memory.summarizer import Summarizer
    from pincer.security.audit import get_audit_logger
    from pincer.security.rate_limiter import get_rate_limiter
    from pincer.tools.bootstrap import register_default_tools
    from pincer.tools.registry import ToolRegistry

    # Initialize components
    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()

    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    # Sprint 5: Security components
    audit_logger = None
    if not settings.audit_disabled:
        audit_logger = await get_audit_logger(settings.db_path)
        console.print("[green]Audit logging enabled[/green]")

    rate_limiter = get_rate_limiter(
        messages_per_minute=settings.rate_messages_per_min,
        tool_calls_per_minute=settings.rate_tools_per_min,
        max_concurrent_llm=settings.max_concurrent_llm,
        max_daily_spend_usd=settings.daily_budget_usd,
    )

    # Initialize memory system
    memory_store: BaseMemoryBackend | None = None
    summarizer: Summarizer | None = None

    # Create LLM provider (primary + optional random failover)
    from pincer.llm.router import LLMRouter

    llm_router = LLMRouter()
    llm = llm_router.get_llm()

    if settings.memory_enabled:
        memory_store = _create_memory_backend(settings)
        await memory_store.initialize()
        summarizer = Summarizer(
            llm=llm_router.get_summarizer(),
            memory_store=memory_store,
            session_manager=session_mgr,
            summary_model=settings.summary_model,
            threshold=settings.summary_threshold,
        )
        console.print("[green]Memory system enabled[/green]")

    # Register channel-independent tools (shell_exec, files, browser,
    # python_exec, email, calendar, Google Workspace, MS365, Slack,
    # generate_image). Channel-bound tools (send_file/send_image, registered
    # by _register_channel_bound_tools), skills, and MCP are wired separately.
    tools = ToolRegistry()
    bootstrap_report = register_default_tools(tools, settings)
    if "google" in bootstrap_report:
        console.print(f"[green]Google Workspace tools enabled ({bootstrap_report['google']} tools)[/green]")
    if "slack" in bootstrap_report:
        console.print(f"[green]Slack tools enabled ({bootstrap_report['slack']} tools)[/green]")
    if "image_gen" in bootstrap_report:
        console.print("[green]Image generation tool registered[/green]")

    # Voice tools are registered BEFORE channel startup: channels that come up
    # early (Telegram) start answering immediately, and a slow channel connect
    # later in the sequence (e.g. WhatsApp waiting 120s on a QR/outdated
    # client) must not leave those first conversations without make_phone_call
    # ("Tool not found"). The voice channel/engine wiring stays in _run_agent.
    if (settings.voice_enabled or settings.voice_outbound_enabled) and settings.twilio_account_sid:
        from pincer.tools.builtin.call_transcript import get_call_transcript
        from pincer.voice.call_tools import register_call_tools
        from pincer.voice.outbound import make_phone_call
        from pincer.voice.threads import init_thread_manager, register_thread_tools

        # Sprint 13: exactly ONE ThreadManager per process (the Hotfix-3
        # lesson — two instances would mean two truths about one thread). It
        # owns the rolling-summary merge, so it gets the same LLM and memory
        # store the post-call pipeline uses.
        init_thread_manager(settings, llm=llm, memory=memory_store)
        register_thread_tools(tools, settings)

        # Sprint 11: the Pincer-owned in-call tools (send_owner_message,
        # memory_note, contact_lookup). Whether the LLM sees them on a call is
        # decided per call by voice.tool_policy (tiers + call scope).
        register_call_tools(tools, settings, memory=memory_store)

        # Sprint 12: the inbound receptionist — the business profile is loaded
        # and validated here (fail fast: a bad profile refuses to start), and
        # business_profile_lookup becomes the line's only knowledge tool.
        if getattr(settings, "receptionist_enabled", False) is True:
            from pincer.voice.receptionist.profile import ProfileError, load_from_settings
            from pincer.voice.receptionist.tools import register_receptionist_tools

            try:
                _profile = load_from_settings(settings)
            except ProfileError as e:
                console.print(f"[red]Receptionist: {e}[/red]")
                raise typer.Exit(1) from e
            register_receptionist_tools(tools)
            if _profile is not None:
                console.print(
                    f"[green]Receptionist enabled for {_profile.business.name} "
                    f"(languages {','.join(_profile.business.languages)})[/green]"
                )

        tools.register(
            name="get_call_transcript",
            description=(
                "Retrieve the transcript of a phone call (PII-masked). Use when the user asks "
                "'show me the transcript of the last call' / 'zeig mir das Transkript' or "
                "references /transcript <CallSid>. Empty call_sid = most recent call."
            ),
            handler=get_call_transcript,
            parameters={
                "type": "object",
                "properties": {
                    "call_sid": {
                        "type": "string",
                        "description": "Twilio Call SID (e.g. CA1234...); empty for the most recent call",
                        "default": "",
                    },
                },
                "required": [],
            },
            require_approval=False,
        )

        if settings.voice_outbound_enabled:

            async def _make_phone_call_from_chat(**kwargs: Any) -> str:
                """The chat-tool entry point. Only difference from the API path
                is the briefing's `source`; both go through make_phone_call."""
                from pincer.voice.briefing import SOURCE_CHAT

                kwargs.setdefault("source", SOURCE_CHAT)
                return await make_phone_call(**kwargs)

            tools.register(
                name="make_phone_call",
                description=(
                    "Place a real phone call to a number. REQUIRED when the user asks you to call someone: "
                    "you MUST call this tool with target_number (E.164) and purpose. "
                    "Do NOT describe or simulate a call in text. Do NOT output XML or structured call blocks. "
                    "Only this tool can place calls. "
                    "Set language='de' when the user writes in German or the callee is German-speaking — "
                    "e.g. 'Ruf beim Zahnarzt an und bestätige den Termin', 'Bitte ruf Herrn Müller an', "
                    "'Sag das Restaurant für morgen ab' → language='de'. "
                    "Set language='uk' when the user writes in Ukrainian or the callee is Ukrainian-speaking — "
                    "e.g. 'Зателефонуй мені', 'Подзвони до лікаря і підтверди запис' → language='uk'. "
                    "English commands ('Call the dentist...') → language='en' (default)."
                ),
                handler=_make_phone_call_from_chat,
                parameters={
                    "type": "object",
                    "properties": {
                        "target_number": {
                            "type": "string",
                            "description": "Phone number in E.164 format (e.g. +14155551234)",
                        },
                        "purpose": {
                            "type": "string",
                            "description": (
                                "REQUIRED, at least 10 characters: the concrete task for this call. The "
                                "agent is bound to it and states it in its first sentence after the "
                                "greeting, so write what should actually be achieved or asked "
                                '("Ask what time they close today"), not a topic label ("opening '
                                'hours"). Write it in the call language.'
                            ),
                        },
                        "instructions": {
                            "type": "string",
                            "description": (
                                "Specific instructions the agent follows during the call "
                                "(what to ask, what to accept, what to avoid)"
                            ),
                            "default": "",
                        },
                        "target_name": {
                            "type": "string",
                            "description": "Name of the person or business being called (optional)",
                            "default": "",
                        },
                        "max_duration": {
                            "type": "integer",
                            "description": "Maximum call duration in seconds (default 300)",
                            "default": 300,
                        },
                        "language": {
                            "type": "string",
                            "enum": ["en", "de", "uk"],
                            "description": (
                                "Language the call is conducted in. 'de' for German commands/callees, "
                                "'uk' for Ukrainian (greeting, conversation, confirmations all in that "
                                "language), 'en' otherwise."
                            ),
                            "default": "en",
                        },
                        "thread_id": {
                            "type": "string",
                            "description": (
                                "Continue an existing call thread when this call follows up on an earlier "
                                "one ('call him again about the appointment'). Find it with thread_lookup. "
                                "If two or more open threads match the contact, ASK the user which one — "
                                "never guess. Empty starts a new thread."
                            ),
                            "default": "",
                        },
                    },
                    "required": ["target_number", "purpose"],
                },
                require_approval=True,
            )

            # Sprint 6: appointment scheduling — free/busy → candidate slots →
            # bounded in-call negotiation → calendar event + invitations.
            from pincer.voice.briefing import SOURCE_CHAT
            from pincer.voice.scheduling import schedule_appointment_call as _schedule_impl

            async def _schedule_appointment_handler(
                target_number: str,
                contact_name: str,
                topic: str,
                timeframe: str,
                duration_minutes: int = 30,
                language: str = "",
                attendees: str = "",
                location_or_meet: str = "",
                thread_id: str = "",
                context: dict | None = None,
            ) -> str:
                ctx = context or {}
                return await _schedule_impl(
                    tools,
                    settings,
                    target_number=target_number,
                    contact_name=contact_name,
                    topic=topic,
                    timeframe=timeframe,
                    duration_minutes=duration_minutes,
                    language=language,
                    attendees=attendees,
                    location_or_meet=location_or_meet,
                    user_id=ctx.get("user_id", ""),
                    channel=ctx.get("channel", ""),
                    thread_id=thread_id,
                    source=SOURCE_CHAT,
                )

            tools.register(
                name="schedule_appointment_call",
                description=(
                    "Call someone to schedule an appointment, negotiating only within the user's real "
                    "free Google Calendar slots, then create the calendar event with invitations and "
                    "report back. Use for commands like 'Call Dr. Smith and schedule an appointment "
                    "next week, 30 minutes' / 'Ruf Dr. Müller an und vereinbare einen Termin nächste "
                    "Woche, 30 Min' (→ language='de') / 'Book a call with the tax advisor tomorrow'. "
                    "Requires Google Calendar to be connected. Prefer this over make_phone_call "
                    "whenever the goal of the call is agreeing on a date/time."
                ),
                handler=_schedule_appointment_handler,
                parameters={
                    "type": "object",
                    "properties": {
                        "target_number": {
                            "type": "string",
                            "description": "Phone number in E.164 format (e.g. +4930123456)",
                        },
                        "contact_name": {
                            "type": "string",
                            "description": "Who is being called (e.g. 'Dr. Müller')",
                        },
                        "topic": {
                            "type": "string",
                            "description": "What the appointment is about, in the call language",
                        },
                        "timeframe": {
                            "type": "string",
                            "description": (
                                "'tomorrow' | 'this_week' | 'next_week' | ISO range "
                                "'YYYY-MM-DD/YYYY-MM-DD' (empty = next 7 days)"
                            ),
                            "default": "",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Appointment length in minutes (default 30)",
                            "default": 30,
                        },
                        "language": {
                            "type": "string",
                            "enum": ["en", "de", "uk"],
                            "description": "Call language; 'de' for German commands/callees",
                            "default": "en",
                        },
                        "attendees": {
                            "type": "string",
                            "description": "Comma-separated emails to invite to the calendar event",
                            "default": "",
                        },
                        "location_or_meet": {
                            "type": "string",
                            "description": "Event location, or 'meet' to attach a Google Meet link",
                            "default": "",
                        },
                        "thread_id": {
                            "type": "string",
                            "description": (
                                "Continue an existing call thread (find it with thread_lookup); empty starts a new one"
                            ),
                            "default": "",
                        },
                    },
                    "required": ["target_number", "contact_name", "topic"],
                },
                require_approval=True,
            )

    # Skills: SKILL.md-based discovery, coexists unconditionally with MCP.
    from pincer.tools.builtin.skills_tools import make_skills_tools
    from pincer.tools.skills.index import BUNDLED_SKILLS_DIR, SkillIndex

    skill_index = SkillIndex(
        bundled_dir=BUNDLED_SKILLS_DIR,
        user_dir=settings.skills_dir,
        max_per_root=settings.skills_max_loaded_per_root,
    )
    skill_index.discover()

    skill_tools = make_skills_tools(skill_index, sandbox_disabled=settings.skill_sandbox_disabled)
    tools.register(
        name="load_skill",
        description="Load a skill's full instructions by name (see Available Skills in the system prompt).",
        handler=skill_tools["load_skill"],
    )
    tools.register(
        name="load_skill_reference",
        description="Read a file referenced by a skill (e.g. a reference doc or data file), by relative path.",
        handler=skill_tools["load_skill_reference"],
    )
    tools.register(
        name="run_skill_script",
        description="Run a script bundled with a skill in a sandboxed subprocess.",
        handler=skill_tools["run_skill_script"],
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name"},
                "script": {"type": "string", "description": "Script path relative to the skill's own directory"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments to pass to the script",
                },
            },
            "required": ["name", "script"],
        },
        require_approval=True,
    )

    if skill_index.all_skills():
        skill_names = ", ".join(e.name for e in skill_index.all_skills())
        console.print(f"[green]Skills indexed: {skill_names}[/green]")
    else:
        console.print("[dim]Skills: none found[/dim]")

    # Load MCP config (coexists with skills — no mutual exclusion).
    _mcp_cfg = None
    try:
        from pincer.mcp import load_mcp_config as _load_mcp_config
        from pincer.mcp.config import _pincer_config_vars

        _mcp_cfg = _load_mcp_config(pincer_vars=_pincer_config_vars(settings))
    except ImportError:
        pass  # mcp package not installed
    except Exception as _mcp_cfg_err:
        console.print(f"[yellow]MCP config load failed — MCP disabled: {_mcp_cfg_err}[/yellow]")

    # MCP client manager (optional — requires mcp package + pincer.toml or env config)
    mcp_manager = None
    try:
        from pincer.mcp import MCPClientManager

        mcp_cfg = _mcp_cfg  # already loaded above; avoids re-reading config files
        if mcp_cfg is not None and mcp_cfg.enabled and mcp_cfg.servers:
            mcp_manager = MCPClientManager(
                config=mcp_cfg,
                tool_registry=tools,
                audit_logger=audit_logger,
                local_upload_root=settings.data_dir / "workspace" / "uploads",
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

    # If memory is backed by MCP but MCP never started, degrade to no-memory
    # rather than letting the agent start with a broken backend that crashes on first write.
    if mcp_manager is None and memory_store is not None:  # pragma: no cover
        from pincer.memory.mcp import MCPMemoryBackend

        if isinstance(memory_store, MCPMemoryBackend):
            console.print(
                "[yellow]Warning: memory_backend=mcp but MCP manager did not start — memory disabled.[/yellow]"
            )
            await memory_store.close()
            memory_store = None
            summarizer = None

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
    agent.skill_index = skill_index
    if mcp_manager:
        agent.mcp_manager = mcp_manager
        from pincer.memory.mcp import MCPMemoryBackend

        if isinstance(memory_store, MCPMemoryBackend):
            memory_store.set_mcp_manager(mcp_manager)

    return CoreComponents(
        session_mgr=session_mgr,
        cost_tracker=cost_tracker,
        audit_logger=audit_logger,
        rate_limiter=rate_limiter,
        llm_router=llm_router,
        llm=llm,
        memory_store=memory_store,
        summarizer=summarizer,
        tools=tools,
        skill_index=skill_index,
        mcp_manager=mcp_manager,
        agent=agent,
    )


def _register_channel_bound_tools(tools: ToolRegistry, channel_map: dict[str, BaseChannel]) -> None:
    """Register send_file/send_image — channel-bound tools only the full agent process needs.

    Kept out of `_build_core` because they close over `channel_map`, which is
    only meaningful in a process that actually starts channels.
    """

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


async def _run_agent(settings: Settings) -> None:
    # Single-instance guard: a second `pincer run` used to keep running with
    # the API port taken — Telegram polling then fights between instances
    # (TelegramConflictError) and Twilio webhooks land on the other process
    # (calls with no audio and no transcript). Fail loudly instead. Lives here
    # (not in _build_core) because `pincer run tasks` workers legitimately run
    # alongside the main process without binding the dashboard port.
    if _port_in_use(settings.dashboard_host, settings.dashboard_port):
        console.print(
            f"[bold red]Port {settings.dashboard_host}:{settings.dashboard_port} is already in use — "
            "another Pincer instance appears to be running.[/bold red]\n"
            "[yellow]Stop it first (e.g. `pkill -f 'pincer run'`) or set PINCER_DASHBOARD_PORT "
            "if something else owns the port.[/yellow]"
        )
        raise typer.Exit(1)

    core = await _build_core(settings)
    session_mgr, cost_tracker, audit_logger, rate_limiter = (
        core.session_mgr,
        core.cost_tracker,
        core.audit_logger,
        core.rate_limiter,
    )
    llm, memory_store = core.llm, core.memory_store
    tools, mcp_manager, agent = core.tools, core.mcp_manager, core.agent

    # send_file / send_image are channel-bound — they need the channel_map
    # populated by channel startup below, so they stay in cli.py rather than
    # in pincer.tools.bootstrap.
    # TODO: channel_map should replaced by router.channels in future but keep stay as is due send_file resolve
    channel_map: dict[str, BaseChannel] = {}
    _register_channel_bound_tools(tools, channel_map)

    # MCP server export (optional — exposes Pincer tools to external MCP clients)
    mcp_server = None

    # Message handler bridge
    async def on_message(incoming: IncomingMessage) -> str:
        from pincer.exceptions import RateLimitExceeded
        from pincer.security.audit import AuditAction, AuditEntry

        canonical_id = incoming.pincer_user_id or incoming.user_id

        # Special commands
        if incoming.text == "/clear":
            session = await session_mgr.get_or_create(canonical_id, incoming.channel)
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
            await rate_limiter.check_message(canonical_id)
        except RateLimitExceeded as e:
            if audit_logger:
                await audit_logger.log(
                    AuditEntry(
                        user_id=canonical_id,
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
                    user_id=canonical_id,
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
                        file_parts.append(_format_pdf_attachment(pages, filename, abs_path))
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

        # Handle image attachments — save to disk so an OCR-capable tool can
        # be given a concrete file_path (mirrors the file-attachment saving
        # above; images arrive without a filename, so one is generated).
        if incoming.images:
            uploads_dir = settings.data_dir / "workspace" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)

            image_parts: list[str] = []
            for raw_bytes, media_type in incoming.images:
                ext = _IMAGE_EXTENSIONS.get(media_type, ".bin")
                filename = f"image_{uuid.uuid4().hex[:8]}{ext}"
                save_path = uploads_dir / filename
                save_path.write_bytes(raw_bytes)
                image_parts.append(_format_image_attachment(filename, str(save_path), len(raw_bytes), media_type))

            image_context = "\n\n".join(image_parts)
            text = f"{image_context}\n\n{text}" if text else image_context

        # The raw ID already on this message is the channel-native ID for
        # whoever we're replying to right now — guaranteed correct, and more
        # reliable than the DB-linked ID below for accounts whose channel
        # identifier can rotate (e.g. WhatsApp LID) or was never fully
        # linked. Same reasoning _raw_id() uses ("most recently seen raw
        # sender") to route approval prompts back to the right JID.
        ch_user_id: str | None = incoming.user_id or None

        if ch_user_id is None:
            # Fallback for the rare case a channel ever constructs an
            # IncomingMessage without a user_id: look up the stable
            # (configured) channel ID so memories still get tagged with both
            # user:{canonical_id} and user:{channel}:{id}.
            try:
                all_ch = await identity.get_all_channels(canonical_id)
                ch_user_id = all_ch.get(incoming.channel_type)
            except Exception:
                pass

            # Recover channel_user_id from a channel-scoped fallback canonical_id
            # (e.g. "teams:{aad_id}" when has_config=True and the user is absent
            # from PINCER_IDENTITY_MAP — no channel_identities row exists for them).
            if ch_user_id is None:
                prefix = f"{incoming.channel_type.value}:"
                if canonical_id.startswith(prefix):
                    ch_user_id = canonical_id[len(prefix) :]

        response = await agent.handle_message(
            user_id=canonical_id,
            channel=incoming.session_id or incoming.channel,
            channel_name=incoming.channel,
            text=text,
            images=incoming.images if incoming.images else None,
            channel_user_id=ch_user_id,
            extra_system=incoming.extra_system,
        )

        # Voice responses are SPOKEN — a cost suffix would be read aloud as
        # "dollar zero point zero zero…" on every turn. Text channels keep it.
        if incoming.channel_type == ChannelType.VOICE:
            return response.text
        cost_str = f"\n\n`${response.cost_usd:.4f}`" if response.cost_usd > 0 else ""
        return response.text + cost_str

    # Sprint 3: Identity resolver
    from pincer.channels.middleware import IdentityMiddleware, build_pipeline
    from pincer.config.identity import resolve_identity_map_config
    from pincer.core.identity import IdentityResolver

    identity_map_config, identity_profiles = resolve_identity_map_config(settings.identity_map)
    identity = IdentityResolver(settings.db_path, identity_map_config, identity_profiles)
    await identity.ensure_table()

    _identity_pipeline = build_pipeline(IdentityMiddleware(identity))

    # Pending ask_user futures per user_id — populated by channels that route
    # ask_user responses back through on_message (WhatsApp).  Telegram uses
    # inline buttons so it owns its own pending state.
    _pending_ask: dict[str, asyncio.Future[str]] = {}

    # Maps "{canonical_id}:{channel}" → raw channel user_id seen in the most
    # recent inbound message.  Used by _raw_id so approval/input prompts are
    # sent back to the exact JID (e.g. LID) the user is currently messaging
    # from, rather than the first-linked ID stored in the DB.
    _active_sender: dict[str, str] = {}

    _orig_on_message_fn = on_message

    async def on_message(incoming: IncomingMessage) -> str:  # type: ignore[no-redef]
        raw_user_id = incoming.user_id  # preserve before pipeline rewrites it
        incoming = await _identity_pipeline(incoming)
        canonical_id = incoming.pincer_user_id or incoming.user_id
        _active_sender[f"{canonical_id}:{incoming.channel}"] = raw_user_id
        fut = _pending_ask.get(canonical_id)
        if fut and not fut.done():
            fut.set_result(incoming.text)
            return ""
        return await _orig_on_message_fn(incoming)

    # Init channels router for proactive delivery
    from pincer.channels.router import ChannelRouter

    router = ChannelRouter(identity)

    # Start channels
    from pincer.channels.base import ChannelType

    tg = None
    wa = None
    ms: MicrosoftTeamsChannel | None = None
    if settings.telegram_bot_token.get_secret_value():
        from pincer.channels.telegram import TelegramChannel

        tg = TelegramChannel(settings)
        tg.set_stream_agent(agent)
        await tg.start(on_message)
        channel_map[tg.name] = tg
        console.print("[green]Telegram connected (streaming enabled)[/green]")
    if tg:
        router.register(ChannelType.TELEGRAM, tg)

    if settings.whatsapp_enabled:
        try:
            from pincer.channels.whatsapp import WhatsAppChannel

            wa = WhatsAppChannel(settings)
            await wa.start(on_message)
            channel_map[wa.name] = wa
            router.register(ChannelType.WHATSAPP, wa)
            console.print("[green]WhatsApp connected[/green]")
        except Exception as e:
            console.print(f"[yellow]WhatsApp failed: {e}[/yellow]")

    # Wire approval + ask_user callbacks across all interactive channels.

    async def _raw_id(canonical_id: str, ch_name: str) -> str:
        """Resolve a canonical pincer_user_id back to the raw channel user ID.

        Priority:
        1. Exact channel-scoped fallback form ("{channel}:{raw_id}").
        2. Most recently seen raw sender for this user/channel (_active_sender).
           This ensures approval/input prompts go back to the exact JID
           (LID or phone) the user is currently messaging from.
        3. First linked channel ID in the identity DB (fallback for proactive).
        """
        prefix = f"{ch_name}:"
        if canonical_id.startswith(prefix):
            return canonical_id[len(prefix) :]
        # Prefer the raw sender from the most recent inbound message so that
        # LID-based WhatsApp accounts route approvals back to the correct JID.
        active = _active_sender.get(f"{canonical_id}:{ch_name}")
        if active:
            return active
        try:
            from pincer.channels.base import ChannelType as _CT

            all_ch = await identity.get_all_channels(canonical_id)
            raw = all_ch.get(_CT(ch_name))
            if raw:
                return raw
        except Exception:
            logger.debug("Failed to resolve raw id for %s/%s", ch_name, canonical_id)
        return canonical_id

    async def _channel_approval(
        tool_name: str,
        arguments: dict,
        user_id: str,
        channel: str,
    ) -> bool:
        if channel == "telegram" and tg is not None:
            raw_id = await _raw_id(user_id, "telegram")
            return await tg.request_approval(raw_id, tool_name, arguments)
        if channel == "whatsapp" and wa is not None:
            raw_id = await _raw_id(user_id, "whatsapp")
            return await wa.request_approval(raw_id, tool_name, arguments)
        if channel == "teams" and ms is not None:
            raw_id = await _raw_id(user_id, "teams")
            return await ms.request_approval(raw_id, tool_name, arguments)
        if channel == "web":
            from pincer.api.approvals import request_web_approval

            return await request_web_approval(tool_name, arguments, user_id, channel)
        logger.warning(
            "No approval UI for channel %s; denying %s",
            channel,
            tool_name,
        )
        return False

    async def _channel_ask_user(user_id: str, channel: str, question: str) -> str:
        if channel == "whatsapp" and wa is not None:
            raw_id = await _raw_id(user_id, "whatsapp")
            return await wa.request_input(
                raw_id,
                f"🔌 *MCP Client* asks:\n\n{question}\n\n_Reply with your answer:_",
            )
        if channel == "telegram" and tg is not None:
            raw_id = await _raw_id(user_id, "telegram")
            fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            _pending_ask[user_id] = fut  # keyed on canonical so on_message lookup works
            try:
                await tg.send(
                    raw_id,
                    f"🔌 **MCP Client** asks:\n\n{question}\n\n_Reply with your answer:_",
                )
                return await asyncio.wait_for(fut, timeout=120)
            except TimeoutError:
                return "[No response from user — timed out after 120s]"
            finally:
                _pending_ask.pop(user_id, None)
        if channel == "teams" and ms is not None:
            raw_id = await _raw_id(user_id, "teams")
            return await ms.request_input(
                raw_id,
                f"🔌 **MCP Client** asks:\n\n{question}\n\n_Reply with your answer:_",
            )
        return "[No supported channel for ask_user]"

    async def _channel_tool_event(
        phase: str,
        tool_name: str,
        arguments: dict,
        user_id: str,
        channel: str,
    ) -> None:
        if channel == "whatsapp" and wa is not None and settings.whatsapp_show_progress:
            try:
                raw_id = await _raw_id(user_id, "whatsapp")
                await wa.notify_tool_event(phase, tool_name, arguments, raw_id)
            except Exception:
                logger.debug("WA tool_event notify failed", exc_info=True)

    if tg is not None or wa is not None or ms is not None:
        agent._approval_callback = _channel_approval
        agent._ask_user_callback = _channel_ask_user
        agent._tool_event_callback = _channel_tool_event

    # Sprint 7: Voice channel (optional)
    # Start when either inbound (voice_enabled) or outbound (voice_outbound_enabled) is enabled
    vc = None
    if (settings.voice_enabled or settings.voice_outbound_enabled) and settings.twilio_account_sid:
        # Sprint 4 (T4.4): validate configured ElevenLabs voice IDs once at
        # startup — a bad ID must fail loudly here, never mid-call. Network
        # trouble only warns; a definitively unknown ID is fatal on
        # media_streams and falls back to the Google voice on ConversationRelay.
        if settings.elevenlabs_api_key.get_secret_value():
            from pincer.voice.voices import validate_configured_voices

            # Sync httpx under the hood — off the event loop, so the already-
            # running channels (Telegram polling, webhooks) keep serving.
            bad_voices = await asyncio.to_thread(validate_configured_voices, settings)
            if bad_voices:
                for vid, problem in bad_voices.items():
                    console.print(f"[bold red]ElevenLabs voice {vid}: {problem}[/bold red]")
                if settings.voice_engine.lower().strip() == "media_streams":
                    console.print(
                        "[bold red]Fix PINCER_ELEVENLABS_VOICE_ID / _EN / _DE "
                        "(find IDs with `pincer voice list`).[/bold red]"
                    )
                    raise typer.Exit(1)
                console.print(
                    "[yellow]ConversationRelay will fall back to the Google voice for affected calls.[/yellow]"
                )
        try:
            from pincer.channels.phone_calls import VoiceChannel
            from pincer.voice.engine import get_voice_engine

            # Dashboard-set runtime overrides (voice turn model) survive restarts
            from pincer.voice.runtime_config import apply_overrides
            from pincer.voice.twiml_server import init_voice_routes

            apply_overrides(settings)

            voice_engine = get_voice_engine(settings)
            vc = VoiceChannel(settings)
            vc.set_engine(voice_engine)
            # Sprint 5: streaming turn pipeline (LLM tokens → sentence
            # boundaries → TTS while the model still writes). The blocking
            # on_message handler stays wired as the guard-regeneration path.
            vc.set_stream_agent(agent)
            vc.set_tool_registry(tools)  # Sprint 12: receptionist free/busy + booking writes
            await vc.start(on_message)
            channel_map[vc.name] = vc
            router.register(ChannelType.VOICE, vc)
            init_voice_routes(voice_engine, settings)
            if getattr(settings, "receptionist_enabled", False) is True:
                from pincer.voice.twiml_server import set_transfer_session_resolver

                set_transfer_session_resolver(vc.get_reception_session)

            # Sprint 9 (T9.1): both call gauges read live engine state.
            from pincer.observability.golden_signals import stuck_calls as _stuck_calls
            from pincer.observability.metrics import set_active_calls_provider, set_stuck_calls_provider

            set_active_calls_provider(lambda: len(voice_engine.get_active_calls()))
            set_stuck_calls_provider(lambda: int(_stuck_calls(settings, voice_engine.get_active_calls()).value or 0))

            # Sprint 1 (T1.5): live call status back to the initiating user's channel
            from pincer.voice.status_notify import set_status_notifier

            async def _voice_status_notifier(user_id: str, channel: str, text: str) -> bool:
                # Dashboard-initiated calls have no push channel — the web UI
                # observes live state and reports via /api/voice. Treat as
                # delivered instead of erroring through the router.
                if channel == "web" or user_id == "dashboard":
                    logging.getLogger("pincer.voice").debug("Dashboard call status (observed via API): %s", text[:120])
                    return True
                try:
                    channel_type = ChannelType(channel)
                except ValueError:
                    channel_type = None
                if channel_type is not None and await router.send(channel_type, user_id, text):
                    return True
                return await router.send_to_user(user_id, text)

            set_status_notifier(_voice_status_notifier)

            # Sprint 11 (`user` mode): the in-call approval card goes to the
            # initiating user's channel; Telegram renders buttons, the
            # dashboard polls /api/voice/approvals, other channels get a
            # text note (no answer path → the gate defers to a follow-up).
            from pincer.voice import approvals as voice_approvals

            async def _present_voice_approval(req: Any) -> bool:
                # Telegram card for telegram-initiated calls — and for inbound
                # (receptionist) calls whose owner id is a Telegram chat id.
                if tg is not None and (req.channel == "telegram" or (not req.channel and str(req.user_id).isdigit())):
                    return await tg.present_voice_approval(req)
                if req.channel == "web" or req.user_id == "dashboard":
                    return True  # answered via POST /api/voice/approvals/{id}
                note = f"📞 In-call approval needed ({req.tool_name}): {req.summary} — answer in the dashboard."
                with contextlib.suppress(Exception):
                    await router.send_to_user(req.user_id, note)
                return False

            async def _finalize_voice_approval(req: Any, final_state: str) -> None:
                if req.channel == "telegram" and tg is not None:
                    await tg.finalize_voice_approval(req, final_state)

            voice_approvals.set_presenter(_present_voice_approval)
            voice_approvals.set_finalizer(_finalize_voice_approval)

            # Sprint 3: post-call intelligence — structured report, memory
            # notes, and follow-up proposals after every call.
            # (make_phone_call / get_call_transcript are registered earlier,
            # before channel startup, so early-started channels have them.)
            from pincer.voice.postcall import PostCallProcessor

            vc.set_post_call_processor(
                PostCallProcessor(
                    settings,
                    llm=llm,
                    memory=memory_store,
                    db_path=str(settings.db_path),
                    tool_registry=tools,  # Sprint 6: appointment calendar executor
                )
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
            dc.set_agent(agent)
            await dc.start(on_message)
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
                await sig.start(on_message)
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
            await slk.start(on_message)
            if slk._app is not None:  # start() may bail silently if import fails
                channel_map[slk.name] = slk
                router.register(ChannelType.SLACK, slk)
                console.print("[green]Slack connected (Socket Mode)[/green]")
            else:
                console.print("[yellow]Slack failed to start (check logs)[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Slack failed: {e}[/yellow]")
    else:
        console.print("[dim]Slack skipped (no PINCER_SLACK_BOT_TOKEN / PINCER_SLACK_APP_TOKEN)[/dim]")

    # Microsoft Teams channel (optional — requires PINCER_TEAMS_APP_ID + PINCER_TEAMS_APP_PASSWORD)
    if settings.teams_app_id and settings.teams_app_password.get_secret_value():
        try:
            from pincer.channels.microsoft_teams import MicrosoftTeamsChannel

            ms = MicrosoftTeamsChannel(settings)
            await ms.start(on_message)
            channel_map[ms.name] = ms
            router.register(ChannelType.TEAMS, ms)
            console.print("[green]Teams connected (mounted under /api/apps/teams)[/green]")
        except Exception as e:
            console.print(f"[yellow]Teams failed: {e}[/yellow]")
    else:
        console.print("[dim]Teams skipped (no PINCER_TEAMS_APP_ID / PINCER_TEAMS_APP_PASSWORD)[/dim]")

    if not router.channels:
        console.print("[yellow]No channels configured. Set PINCER_TELEGRAM_BOT_TOKEN.[/yellow]")
        return

    # normalize identity map in through router
    await router.rebuild_identity_map()

    # MCP servers may push notifications for work that outlives the tool call
    # that triggered it (e.g. ms365-mcp's delayed auth completion) — the
    # channel router doesn't exist yet when MCPClientManager is constructed
    # above, so this is wired up as late-binding here.
    if mcp_manager is not None:
        from pincer.mcp.notifications import create_auth_notification_handler

        mcp_manager.set_notification_handler(create_auth_notification_handler(router))

    # Sprint 3: Scheduler + Proactive Agent
    from pincer.scheduler import CronScheduler, EventTriggerManager, ProactiveAgent

    proactive = ProactiveAgent(settings.db_path, agent=agent)
    await proactive.ensure_table()

    scheduler = CronScheduler(settings.db_path)
    await scheduler.ensure_table()

    # #170: actors deliver results through a pub/sub bridge rather than
    # calling the router directly — the same DeliveryBackend/ResultEmitter
    # code path a standalone `pincer run tasks` worker uses, so this
    # in-process default and a split deployment behave identically.
    # See pincer.tasks.delivery. (The voice retention_purge action is handled
    # in pincer.tasks.actors alongside briefing/custom.)
    from pincer.tasks.delivery import ResultEmitter, ResultRelay, create_delivery_backend

    delivery_backend = create_delivery_backend(settings)
    deliverer = ResultEmitter(delivery_backend)

    if (settings.voice_enabled or settings.voice_outbound_enabled) and settings.voice_transcript_retention_days > 0:
        try:
            purge_tz = settings.voice_timezone or settings.timezone
            purge_user = settings.default_user_id or "system"
            existing = await scheduler.list_schedules(purge_user)
            if not any(s["name"] == "voice_retention_purge" for s in existing):
                await scheduler.add(
                    name="voice_retention_purge",
                    cron_expr="30 3 * * *",
                    action={"type": "retention_purge"},
                    pincer_user_id=purge_user,
                    tz=purge_tz,
                )
                console.print(
                    f"[green]Voice transcript retention purge scheduled daily "
                    f"(retention={settings.voice_transcript_retention_days}d, tz={purge_tz})[/green]"
                )
        except Exception as e:
            console.print(f"[yellow]Retention schedule error: {e}[/yellow]")

    # Sprint 13 §5: threads with no activity for PINCER_THREAD_AUTOCLOSE_DAYS
    # are closed automatically. Idempotent by name, like every schedule here.
    if (settings.voice_enabled or settings.voice_outbound_enabled) and settings.thread_autoclose_days > 0:
        try:
            thread_user = settings.default_user_id or "system"
            existing = await scheduler.list_schedules(thread_user)
            if not any(s["name"] == "voice_thread_autoclose" for s in existing):
                await scheduler.add(
                    name="voice_thread_autoclose",
                    cron_expr="45 3 * * *",
                    action={"type": "thread_autoclose"},
                    pincer_user_id=thread_user,
                    tz=settings.voice_timezone or settings.timezone,
                )
                console.print(
                    f"[green]Call-thread auto-close scheduled daily "
                    f"(after {settings.thread_autoclose_days}d of inactivity)[/green]"
                )
        except Exception as e:
            console.print(f"[yellow]Thread auto-close schedule error: {e}[/yellow]")

    # Sprint 9 (T9.2): the three scheduled observability jobs. Each is
    # idempotent by name, so a restart never duplicates a schedule, and each is
    # only created when its prerequisite is actually configured — an alert
    # scanner with nowhere to deliver is worse than no scanner.
    await _ensure_ops_schedules(scheduler, settings)

    # Sprint 3: Event triggers
    triggers = EventTriggerManager(settings.db_path, deliverer)
    await triggers.start()

    # Sprint 6: Background task execution (repid) — actors run scheduled and
    # on-request work durably/retryably; the dispatcher below keeps the SQLite
    # cron poll loop, since repid has no native scheduler to replace it with.
    from pincer.tasks import ScheduleDispatcher, register_default_server
    from pincer.tasks import app as task_app
    from pincer.tasks.context import set_context

    set_context(deliverer, proactive, triggers)
    register_default_server()
    task_connection = task_app.servers.default.connection()
    await task_connection.__aenter__()
    task_worker = asyncio.create_task(
        # register_signals=[] — repid defaults to installing its own SIGINT/SIGTERM
        # handlers via loop.add_signal_handler(), which replaces Python's default
        # signal handling process-wide and swallows the Ctrl+C / SIGTERM this
        # process's own shutdown sequence (below) depends on. cli.py already owns
        # signal handling end-to-end via task_worker.cancel() in the finally block.
        task_app.run_worker(graceful_shutdown_time=10.0, register_signals=[]),
        name="pincer-task-worker",
    )
    dispatcher = ScheduleDispatcher(scheduler, task_app, interval=settings.task_poll_interval)
    await dispatcher.start()

    result_relay = ResultRelay(delivery_backend, router)
    await result_relay.start()

    console.print(f"[green]Task worker started (broker={settings.task_broker})[/green]")

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
        from pincer.mcp.config import _pincer_config_vars

        _mcp_full_cfg = _load_mcp_cfg(pincer_vars=_pincer_config_vars(settings))
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
    tunnel = None
    _api_will_run = not _port_in_use(settings.dashboard_host, settings.dashboard_port)
    if not _api_will_run:
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
            # Share the fully-built CLI agent (with MCP manager and memory
            # backend already wired) so the API server doesn't create a second
            # disconnected instance.
            api_app.state.agent = agent
            api_app.state.teams_channel = ms
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
            if settings.ngrok_authtoken.get_secret_value():
                from pincer.tunnel.ngrok import NgrokTunnel

                tunnel = NgrokTunnel(
                    authtoken=settings.ngrok_authtoken.get_secret_value(),
                    domain=settings.ngrok_domain,
                    target_port=settings.dashboard_port,
                )
                public_url = await tunnel.start()
                if public_url:
                    console.print(f"[green]Ngrok tunnel: {public_url}[/green]")
        except Exception as e:
            console.print(f"[yellow]API server failed to start: {e}[/yellow]")

    if settings.voice_enabled or settings.voice_outbound_enabled:
        _print_voice_webhook_urls(settings, console)

    active = [ch.name for ch in router.channels.values()]
    console.print(
        f"\n[bold green]{settings.agent_name} is running![/bold green] "
        f"Channels: {', '.join(active)}. Press Ctrl+C to stop.\n"
    )

    # T7.2: `docker stop` / a deploy sends SIGTERM. Without a handler Python
    # dies mid-call (no spoken ending, no report). Route it into the same
    # graceful path as Ctrl+C: cancel the main task -> finally block ->
    # channel.stop() drains active calls with a spoken ending.
    import signal as _sigterm_mod

    _main_task = asyncio.current_task()
    with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows / nested loops
        asyncio.get_running_loop().add_signal_handler(
            _sigterm_mod.SIGTERM,
            lambda: _main_task.cancel() if _main_task and not _main_task.done() else None,
        )

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/yellow]")
    finally:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
        if tunnel is not None:
            await tunnel.stop()
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
        await dispatcher.stop()
        await result_relay.stop()
        # repid's own worker logs CRITICAL + a full traceback on CancelledError
        # (by design — see repid/_worker.py, it re-raises after logging). That's
        # expected noise here since this cancel() is our own intentional shutdown,
        # not an unexpected failure — silence it for this one call.
        logging.getLogger("repid").setLevel(logging.CRITICAL + 1)
        task_worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_worker
        await task_connection.__aexit__(None, None, None)
        await proactive.close()
        for ch in router.channels.values():
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


async def _run_tasks_worker(settings: Settings) -> None:
    """Standalone background-task worker: consumes the repid queue, no channels/API/MCP export.

    Requires `task_broker=redis` (checked by the caller) since the in-memory
    broker can't be shared with whatever process is enqueuing work. Actors
    deliver results through the same `ResultEmitter`/`DeliveryBackend` path
    `_run_agent` uses — see `pincer.tasks.delivery` — so a `ResultRelay` in
    the main `pincer run` process picks them up and delivers via its own,
    already-started channels.
    """
    from pincer.scheduler import EventTriggerManager, ProactiveAgent
    from pincer.tasks import TASK_CHANNEL, register_default_server
    from pincer.tasks import app as task_app
    from pincer.tasks.context import set_context
    from pincer.tasks.delivery import TASK_RESULTS_CHANNEL, ResultEmitter, create_delivery_backend

    core = await _build_core(settings)

    proactive = ProactiveAgent(settings.db_path, agent=core.agent)
    await proactive.ensure_table()

    delivery_backend = create_delivery_backend(settings)
    deliverer = ResultEmitter(delivery_backend)

    # ensure_table() only, never .start() — EventTriggerManager's email/calendar
    # polling loops are intentionally NOT part of the repid migration: they
    # remain single-process asyncio.create_task loops by design (see
    # EventTriggerManager.start()) and must run in exactly one process (the
    # main `pincer run` process). This worker only needs ensure_table() for the
    # process_webhook actor's handle_webhook() call — that actor isn't wired to
    # a production trigger yet (nothing enqueues it outside of tests).
    triggers = EventTriggerManager(settings.db_path, deliverer)
    await triggers.ensure_table()

    set_context(deliverer, proactive, triggers)
    register_default_server()
    task_connection = task_app.servers.default.connection()
    await task_connection.__aenter__()
    task_worker = asyncio.create_task(
        task_app.run_worker(graceful_shutdown_time=10.0, register_signals=[]),
        name="pincer-tasks-worker",
    )

    console.print(f"[bold green]{settings.agent_name} tasks worker starting...[/bold green]")
    console.print(f"   Broker: redis ({settings.task_broker_url})")
    console.print(
        f"[green]Task worker running (channel={TASK_CHANNEL}, results relayed via {TASK_RESULTS_CHANNEL})[/green]"
    )
    console.print("\n[bold green]Tasks worker is running![/bold green] Press Ctrl+C to stop.\n")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/yellow]")
    finally:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
        logging.getLogger("repid").setLevel(logging.CRITICAL + 1)
        task_worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_worker
        await task_connection.__aexit__(None, None, None)
        await delivery_backend.aclose()
        await proactive.close()
        if core.mcp_manager:
            await core.mcp_manager.stop()
        try:
            from pincer.tools.builtin.browser import close_browser

            await close_browser()
        except ImportError:
            pass
        await core.llm.close()
        await core.session_mgr.close()
        await core.cost_tracker.close()
        if core.memory_store:
            await core.memory_store.close()
        if core.audit_logger:
            await core.audit_logger.shutdown()
        console.print("[green]Tasks worker shutdown complete[/green]")
        _pending = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        for _t in _pending:
            _t.cancel()
        os._exit(0)


@app.command()
def config() -> None:
    """Show current configuration."""
    from pincer.config import get_settings

    try:
        settings = get_settings()
        console.print("[bold]Pincer Configuration[/bold]\n")
        console.print(f"  Provider:     {settings.default_provider}")
        console.print(f"  Model:        {settings.default_model}")
        console.print(f"  Anthropic:    {'set' if settings.anthropic_api_key.get_secret_value() else 'not set'}")
        console.print(f"  OpenAI:       {'set' if settings.openai_api_key.get_secret_value() else 'not set'}")
        console.print(f"  Telegram:     {'set' if settings.telegram_bot_token.get_secret_value() else 'not set'}")
        console.print(f"  Budget:       ${settings.daily_budget_usd:.2f}/day")
        console.print(f"  Data dir:     {settings.data_dir}")
        console.print(f"  Shell:        {'enabled' if settings.shell_enabled else 'disabled'}")
        console.print(f"  Log level:    {settings.log_level.value}")
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

    settings = get_settings_relaxed()
    tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await tracker.initialize()

    today = await tracker.get_today_spend()
    summary = await tracker.get_summary()

    console.print("[bold]Pincer Cost Report[/bold]\n")
    console.print(f"  Today:   ${today:.4f} / ${settings.daily_budget_usd:.2f}")
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
        choices=["anthropic", "openai", "both", "compatible"],
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
    if provider == "compatible":
        # Any OpenAI-/Anthropic-wire endpoint (Grok, Ollama, local proxy, gateway).
        wire = Prompt.ask("Wire format", choices=["openai", "anthropic"], default="openai")
        name = Prompt.ask("Provider name (e.g. grok, ollama, my-claude)")
        base_url = Prompt.ask("Base URL")
        key = Prompt.ask("API key (empty if not required)", password=True, default="")
        model = Prompt.ask("Model id (e.g. grok-3, llama3.2)")
        prefix = "OPENAI" if wire == "openai" else "ANTHROPIC"
        env_lines.append(f"PINCER_DEFAULT_PROVIDER={name}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_PROVIDER={name}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_BASE_URL={base_url}")
        if key:
            env_lines.append(f"PINCER_{prefix}_COMPATIBLE_API_KEY={key}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_MODEL={model}")

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
    production: bool = typer.Option(
        False,
        "--production",
        help="Deploy gate (Sprint 7): adds production checks and exits non-zero on any CRITICAL",
    ),
) -> None:
    """Run 25+ security checks with traffic-light report."""
    import json as _json
    from pathlib import Path as _P

    from pincer.security.doctor import CheckStatus, SecurityDoctor

    doc = SecurityDoctor(
        data_dir=_P("data"),
        config_dir=_P("."),
        production=production,
    )
    report = doc.run_all()

    if output_json:
        console.print(_json.dumps(report.to_dict(), indent=2))
        if production and report.critical > 0:
            raise typer.Exit(1)
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
    if production and report.critical > 0:
        console.print("[bold red]Production gate: RED — refusing (deploy scripts must not start).[/bold red]")
        raise typer.Exit(1)


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
    from pincer.memory.summarizer import Summarizer
    from pincer.tools.builtin.files import file_list, file_read, file_write
    from pincer.tools.registry import ToolRegistry

    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()
    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    from pincer.llm.router import LLMRouter

    llm_router = LLMRouter()
    llm = llm_router.get_llm()

    memory_store: BaseMemoryBackend | None = None
    summarizer: Summarizer | None = None
    if settings.memory_enabled:
        memory_store = _create_memory_backend(settings)
        await memory_store.initialize()
        from pincer.memory.mcp import MCPMemoryBackend

        if isinstance(memory_store, MCPMemoryBackend):
            console.print(
                "[yellow]Warning: memory_backend=mcp is not supported "
                "in interactive CLI mode — memory disabled.[/yellow]"
            )
            await memory_store.close()
            memory_store = None
        else:
            summarizer = Summarizer(
                llm=llm_router.get_summarizer(),
                memory_store=memory_store,
                session_manager=session_mgr,
                summary_model=settings.summary_model,
                threshold=settings.summary_threshold,
            )

    tools = ToolRegistry()
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

# ── Voice subcommands (Sprint 4) ──────────────────────────

voice_app = typer.Typer(name="voice", help="Manage ElevenLabs voices for voice calling")
app.add_typer(voice_app, name="voice")


pilot_app = typer.Typer(name="pilot", help="Pilot onboarding and review tooling (Sprint 10)")
app.add_typer(pilot_app, name="pilot")


@pilot_app.command(name="preflight")
def pilot_preflight(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """Check every onboarding prerequisite against the live configuration.

    Run this BEFORE the customer is on the call — discovering a missing Twilio
    number mid-session is what turns a 2h onboarding into a 4h one."""
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.onboarding import (
        TARGET_MINUTES,
        StepStatus,
        blocking,
        manual_minutes,
        preflight,
        total_minutes,
    )

    results = preflight(get_settings_relaxed())
    if output_json:
        console.print(_json.dumps([r.to_dict() for r in results], indent=2))
        raise typer.Exit(1 if blocking(results) else 0)

    icons = {
        StepStatus.READY: "[green]✅[/green]",
        StepStatus.MISSING: "[red]❌[/red]",
        StepStatus.MANUAL: "[cyan]☐[/cyan]",
        StepStatus.SKIPPED: "[dim]–[/dim]",
    }
    table = Table(show_header=True, title="Onboarding preflight")
    table.add_column("")
    table.add_column("Step")
    table.add_column("Min", justify="right")
    table.add_column("Detail")
    for result in results:
        table.add_row(icons[result.status], result.step.title, str(result.step.minutes), result.message)
    console.print(table)

    missing = blocking(results)
    console.print(
        f"\nEstimated: [bold]{total_minutes()} min[/bold] "
        f"({manual_minutes()} manual) against a {TARGET_MINUTES} min target."
    )
    if missing:
        console.print(f"\n[red]{len(missing)} blocking item(s) — fix before the onboarding session.[/red]\n")
    else:
        console.print("\n[green]No blocking items. Manual steps (☐) still need a human.[/green]\n")
    raise typer.Exit(1 if missing else 0)


@pilot_app.command(name="checklist")
def pilot_checklist(
    customer: str = typer.Argument(..., help="Customer name, used as the document title"),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Emit a per-customer onboarding checklist with a time-tracking table."""
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.onboarding import preflight, render_checklist

    markdown = render_checklist(customer, preflight(get_settings_relaxed()))
    if out:
        _Path(out).write_text(markdown, encoding="utf-8")
        console.print(f"[green]Checklist written to {out}[/green]")
    else:
        console.print(markdown)


@pilot_app.command(name="spot-check")
def pilot_spot_check(
    count: int = typer.Option(10, "--count", "-n", help="Calls to sample"),
    days: int = typer.Option(7, "--days", "-d", help="Window in days"),
    seed: int = typer.Option(0, "--seed", help="Sampling seed — same seed, same calls"),
    language: str = typer.Option("", "--language", "-l", help="Only calls in this language"),
    failures_only: bool = typer.Option(False, "--failures-only", help="Only calls that did not complete"),
    week: str = typer.Option("", "--week", help="Label for the sheet (e.g. 'week 2')"),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Weekly transcript spot-check sheet (T10.2).

    Ten calls, PII-masked, with the review questions attached. The sample is
    deterministic from --seed so two reviewers argue about the same calls."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.observability.pilot_review import render_spot_check, sample_calls

    calls = _asyncio.run(
        sample_calls(
            get_settings_relaxed(),
            count=count,
            days=days,
            seed=seed,
            language=language or None,
            only_failures=failures_only,
        )
    )
    if not calls:
        console.print(f"[yellow]No calls in the last {days} day(s) matching the filter.[/yellow]")
        raise typer.Exit(1)

    sheet = render_spot_check(calls, week=week)
    if out:
        _Path(out).write_text(sheet, encoding="utf-8")
        console.print(f"[green]Spot-check sheet ({len(calls)} calls) written to {out}[/green]")
    else:
        console.print(sheet)


@pilot_app.command(name="export-fixture")
def pilot_export_fixture(
    call_sid: str = typer.Argument(..., help="Call SID to turn into a harness persona"),
    name: str = typer.Option("", "--name", help="Fixture name (default: derived from the SID)"),
    notes: str = typer.Option("", "--notes", help="Why this call is worth replaying"),
    out: str = typer.Option("", "--out", "-o", help="Directory or file to write the fixture to"),
) -> None:
    """Export a real call as a PII-masked harness persona fixture (T10.3).

    Review the flagged names before committing — mask_pii handles numbers and
    emails, but personal names are not a pattern."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.observability.pilot_review import export_persona_fixture, fixture_to_json

    try:
        fixture = _asyncio.run(export_persona_fixture(get_settings_relaxed(), call_sid, name=name, notes=notes))
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    payload = fixture_to_json(fixture)
    if out:
        target = _Path(out)
        if target.is_dir():
            target = target / f"{fixture['name']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        console.print(f"[green]Fixture written to {target}[/green]")
    else:
        console.print(payload)

    flagged = fixture["review_required"]["possible_names"]
    if flagged:
        console.print(
            f"\n[yellow]⚠️  Possible personal names detected: {', '.join(flagged)}[/yellow]\n"
            "[yellow]Replace them with placeholders and empty `review_required.possible_names` "
            "before committing — the loader refuses an unreviewed fixture.[/yellow]\n"
        )
    else:
        console.print("\n[green]No personal names detected. Still read it once before committing.[/green]\n")


@pilot_app.command(name="automation-candidates")
def pilot_automation_candidates() -> None:
    """Manual onboarding steps ranked by minutes saved (T10.3 input).

    This is the list "automate the top 3 manual steps" refers to. It is derived
    from the step timings, so it changes when reality does."""
    from rich.table import Table

    from pincer.onboarding import (
        TARGET_MINUTES,
        automatable_minutes,
        automation_candidates,
        manual_minutes,
        total_minutes,
    )

    candidates = automation_candidates()
    table = Table(show_header=True, title="Onboarding automation candidates")
    table.add_column("Min saved", justify="right")
    table.add_column("Step")
    table.add_column("What automating it takes")
    for step in candidates:
        table.add_row(str(step.minutes), step.title, step.automation_note)
    console.print(table)

    remaining = total_minutes() - automatable_minutes()
    console.print(
        f"\nNow: [bold]{total_minutes()} min[/bold] ({manual_minutes()} manual). "
        f"Automating all {len(candidates)} candidates would reach ~{remaining} min "
        f"against the {TARGET_MINUTES} min target.\n"
    )


ops_app = typer.Typer(name="ops", help="Voice operations: golden signals, SLOs, canary, digest (Sprint 9)")
voice_app.add_typer(ops_app, name="ops")


def _signal_row(signal: dict) -> tuple[str, str, str, str]:
    """(name, value, target, sample) formatted for the golden-signal table."""
    value, unit, target = signal.get("value"), signal.get("unit", ""), signal.get("target")
    if not signal.get("sufficient_data"):
        shown = f"[dim]n/a ({signal.get('sample_size', 0)}/{signal.get('min_sample', 1)} samples)[/dim]"
    elif unit == "ratio":
        shown = f"{value:.1%}"
    elif unit == "count":
        shown = str(int(value or 0))
    elif unit == "ratio_to_baseline":
        shown = f"{value:.2f}×"
    else:
        shown = f"{value:.2f}{unit}"
    goal = ""
    if target is not None:
        if unit == "ratio":
            goal = f"{target:.0%}"
        elif unit == "ratio_to_baseline":
            goal = f"{target:g}× baseline"
        elif unit == "count":
            goal = f"{target:g}"
        else:
            goal = f"{target:g}{unit}"
    return signal.get("name", ""), shown, goal, f"{signal.get('sample_size', 0)} / {signal.get('window', '')}"


@ops_app.command(name="status")
def voice_ops_status(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """The five golden signals and any alert that would fire right now.

    First command on the on-call quick card — one screen that answers
    "is voice healthy?" without a browser or a metrics backend."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability import golden_signals as _gs
    from pincer.observability.alerts import disk_alert, evaluate

    settings = get_settings_relaxed()

    async def _collect():
        signals = await _gs.collect(settings)
        alerts = evaluate(signals, settings)
        host = disk_alert(settings)
        if host is not None:
            alerts.insert(0, host)
        return signals, alerts

    signals, alerts = _asyncio.run(_collect())

    if output_json:
        console.print(
            _json.dumps(
                {
                    **signals.to_dict(),
                    "alerts": [
                        {"rule": a.rule, "severity": str(a.severity), "title": a.title, "detail": a.detail}
                        for a in alerts
                    ],
                },
                indent=2,
            )
        )
        return

    table = Table(show_header=True, title="Voice golden signals")
    table.add_column("Signal")
    table.add_column("Value", justify="right")
    table.add_column("Threshold", justify="right")
    table.add_column("Sample / window")
    for signal in signals.to_dict()["signals"].values():
        table.add_row(*_signal_row(signal))
    console.print(table)

    if not alerts:
        console.print("\n[green]No alerts firing.[/green]\n")
        return
    console.print("")
    for alert in alerts:
        color = "red" if str(alert.severity) == "page" else "yellow"
        console.print(f"[{color}]{alert.render()}[/{color}]\n")


@ops_app.command(name="slo")
def voice_ops_slo(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """Month-to-date SLO status and error-budget burn (T9.5)."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.slo import collect

    report = _asyncio.run(collect(get_settings_relaxed()))
    if output_json:
        console.print(_json.dumps(report, indent=2))
        return

    table = Table(show_header=True, title=f"SLOs — {report['slos'][0]['window'] if report['slos'] else ''}")
    table.add_column("SLO")
    table.add_column("Actual", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Budget burned", justify="right")
    table.add_column("n", justify="right")
    for slo in report["slos"]:
        actual, unit = slo["actual"], slo["unit"]
        if actual is None:
            shown = "[dim]no data[/dim]"
        elif unit == "ratio":
            shown = f"{actual:.2%}"
        else:
            shown = f"{actual:.2f}{unit}"
        target = f"{slo['target']:.1%}" if unit == "ratio" else f"{slo['target']:g}{unit}"
        burn = slo["burn_pct"]
        burn_text = "[dim]—[/dim]" if burn is None else f"{'[red]' if burn > 100 else ''}{burn:.0f}%"
        label = slo["name"] + (" [dim](inferred)[/dim]" if slo["confidence"] == "inferred" else "")
        table.add_row(label, shown, target, burn_text, str(slo["sample_size"]))
    console.print(table)

    if report["feature_freeze"]:
        console.print(f"\n[red]🧊 Feature freeze in effect: {report['freeze_reason']}[/red]\n")
    else:
        console.print(
            f"\n[green]No feature freeze[/green] "
            f"[dim](threshold {report['freeze_threshold_pct']:.0f}% burn, "
            f"min {report['freeze_min_sample']} samples)[/dim]\n"
        )


@ops_app.command(name="canary")
def voice_ops_canary(
    history: bool = typer.Option(False, "--history", help="Show recent runs instead of placing a call"),
) -> None:
    """Run the synthetic canary call now, or show recent runs.

    Without --history this places a REAL phone call to
    PINCER_VOICE_CANARY_NUMBER through the normal abuse gate."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.canary import recent_runs, run_canary

    settings = get_settings_relaxed()

    if history:
        runs = _asyncio.run(recent_runs(settings, limit=20))
        if not runs:
            console.print("[yellow]No canary runs recorded yet.[/yellow]")
            return
        table = Table(show_header=True, title="Canary runs")
        table.add_column("When")
        table.add_column("Result")
        table.add_column("Turns", justify="right")
        table.add_column("Duration", justify="right")
        table.add_column("Reason")
        for run in runs:
            if run["skipped"]:
                result = "[yellow]skipped[/yellow]"
            elif run["ok"]:
                result = "[green]ok[/green]"
            else:
                result = "[red]FAILED[/red]"
            table.add_row(
                str(run["ran_at"])[:19],
                result,
                str(run["turns"]),
                f"{run['duration_s']:.0f}s",
                str(run["reason"] or "")[:60],
            )
        console.print(table)
        return

    if not settings.voice_canary_enabled:
        console.print("[yellow]Canary is disabled. Set PINCER_VOICE_CANARY_ENABLED=true.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[cyan]Placing canary call to {settings.voice_canary_number}...[/cyan]")
    result = _asyncio.run(run_canary(settings))
    console.print(result.render())
    if not result.ok:
        raise typer.Exit(1)


@ops_app.command(name="ga-gate")
def voice_ops_ga_gate(
    days: int = typer.Option(14, "--days", "-d", help="Pilot window in days"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output the sign-off document"),
    out: str = typer.Option("", "--out", "-o", help="Write the markdown report to this file"),
) -> None:
    """Evaluate the GA exit criteria against real pilot data (Sprint 10, T10.4).

    Exit code 0 only when EVERY criterion passes — 'insufficient data' is not a
    pass, so this is safe to wire into a release gate."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.ga_gate import Verdict, evaluate, render_markdown

    report = _asyncio.run(evaluate(get_settings_relaxed(), days=days))

    if out:
        from pathlib import Path as _Path

        _Path(out).write_text(render_markdown(report), encoding="utf-8")
        console.print(f"[green]Sign-off document written to {out}[/green]")
    if output_json:
        console.print(_json.dumps(report.to_dict(), indent=2, default=str))
    elif markdown:
        console.print(render_markdown(report))
    else:
        icons = {
            Verdict.PASS: "[green]✅ pass[/green]",
            Verdict.FAIL: "[red]❌ fail[/red]",
            Verdict.INSUFFICIENT: "[yellow]⏳ no data[/yellow]",
            Verdict.MANUAL: "[cyan]🧑 manual[/cyan]",
        }
        table = Table(show_header=True, title=f"GA gate — last {days} days")
        table.add_column("Result")
        table.add_column("Criterion")
        table.add_column("Evidence")
        for criterion in report.criteria:
            table.add_row(icons[criterion.verdict], criterion.title, criterion.summary)
        console.print(table)

        if report.ready:
            console.print("\n[green]✅ READY FOR GA — every criterion met.[/green]\n")
        else:
            console.print(
                f"\n[red]🚫 NOT READY[/red] — {len(report.failed)} failed, {len(report.blocked)} undecided.\n"
            )
            for criterion in report.failed + report.blocked:
                if criterion.needed:
                    console.print(f"  [dim]{criterion.key}:[/dim] {criterion.needed}")
            console.print("")

    raise typer.Exit(0 if report.ready else 1)


@ops_app.command(name="digest")
def voice_ops_digest() -> None:
    """Render the weekly failure digest without sending it."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.observability.digest import build_digest

    console.print(_asyncio.run(build_digest(get_settings_relaxed())))


@ops_app.command(name="failures")
def voice_ops_failures(
    hours: float = typer.Option(168.0, "--hours", "-h", help="Window in hours (default: 7 days)"),
) -> None:
    """Failure codes over a window, ranked (T9.3)."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.failure_codes import describe
    from pincer.observability.golden_signals import call_success_rate

    signal = _asyncio.run(call_success_rate(get_settings_relaxed(), window_hours=hours))
    by_code = signal.detail.get("by_failure_code") or {}
    if not by_code:
        console.print(f"[yellow]No terminated calls in the last {hours:g}h.[/yellow]")
        return

    table = Table(show_header=True, title=f"Failure codes — last {hours:g}h ({signal.sample_size} calls)")
    table.add_column("Code")
    table.add_column("Count", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Meaning")
    for code, count in sorted(by_code.items(), key=lambda kv: kv[1], reverse=True):
        share = count / signal.sample_size if signal.sample_size else 0.0
        table.add_row(code, str(count), f"{share:.0%}", describe(code))
    console.print(table)


dnc_app = typer.Typer(name="dnc", help="Do-not-call list — numbers Pincer will never dial (Sprint 8, T8.3)")
voice_app.add_typer(dnc_app, name="dnc")


@dnc_app.command(name="list")
def voice_dnc_list() -> None:
    """Show the shared do-not-call list (blocks every user and channel)."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.voice.safety_gates import list_do_not_call

    entries = _asyncio.run(list_do_not_call(get_settings_relaxed()))
    if not entries:
        console.print("[green]Do-not-call list is empty.[/green]")
        return

    table = Table(show_header=True, title=f"Do-not-call list ({len(entries)} number(s))")
    table.add_column("Number")
    table.add_column("Source")
    table.add_column("Reason")
    table.add_column("Added")
    for entry in entries:
        table.add_row(
            entry.get("phone_number", ""),
            entry.get("source", "") or "-",
            entry.get("reason", "") or "-",
            (entry.get("added_at", "") or "")[:19],
        )
    console.print(table)


@dnc_app.command(name="add")
def voice_dnc_add(
    number: str = typer.Argument(..., help="Phone number in E.164 format, e.g. +4915112345678"),
    reason: str = typer.Option("", "--reason", "-r", help="Why this number is blocked"),
) -> None:
    """Block a number. Applies immediately to every channel and every user."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.voice.outbound import validate_e164
    from pincer.voice.safety_gates import add_do_not_call

    validated = validate_e164(number)
    if not validated:
        console.print(f"[red]Invalid phone number: {number}. Use E.164 format (e.g. +4915112345678).[/red]")
        raise typer.Exit(1)

    added = _asyncio.run(add_do_not_call(get_settings_relaxed(), validated, reason=reason, source="cli"))
    if added:
        console.print(f"[green]{validated} added to the do-not-call list.[/green]")
    else:
        console.print(f"[yellow]{validated} was already on the do-not-call list (reason updated).[/yellow]")


@dnc_app.command(name="remove")
def voice_dnc_remove(
    number: str = typer.Argument(..., help="Phone number to unblock"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Unblock a number — only ever do this with the callee's consent."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.voice.safety_gates import remove_do_not_call

    if not yes and not typer.confirm(f"Remove {number} from the do-not-call list?"):
        raise typer.Abort

    removed = _asyncio.run(remove_do_not_call(get_settings_relaxed(), number))
    if removed:
        console.print(f"[green]{number} removed from the do-not-call list.[/green]")
    else:
        console.print(f"[yellow]{number} was not on the do-not-call list.[/yellow]")
        raise typer.Exit(1)


@voice_app.command(name="latency-report")
def voice_latency_report(
    calls: int = typer.Option(20, "--calls", "-n", help="Number of most recent calls to analyze"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """p50/p95 per latency stage from recent voice turns (Sprint 5, T5.1).

    Reads data/logs/voice_latency.jsonl, written one line per streamed turn."""
    import json as _json

    from pincer.config import get_settings_relaxed
    from pincer.voice.latency_report import build_latency_report, read_turn_records

    settings = get_settings_relaxed()
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    records = read_turn_records(path, last_calls=calls)
    if not records:
        console.print(f"[yellow]No turn records in {path} — run some calls first.[/yellow]")
        raise typer.Exit(1)

    report = build_latency_report(records)
    if output_json:
        console.print(_json.dumps(report, indent=2))
        return

    from rich.table import Table

    header = (
        f"\n[bold]Voice latency report[/bold] — {report['turns']} turn(s) "
        f"across {report['calls']} call(s), engines: {', '.join(report['engines']) or '-'}\n"
    )
    console.print(header)
    table = Table(show_header=True)
    table.add_column("Stage")
    table.add_column("p50 ms", justify="right")
    table.add_column("p95 ms", justify="right")
    table.add_column("n", justify="right")
    for stage, stats in report["stages"].items():
        table.add_row(stage, f"{stats['p50']:.0f}", f"{stats['p95']:.0f}", str(stats["n"]))
    console.print(table)
    total = report["stages"].get("total_ms")
    if total:
        target_ok = total["p50"] <= 1200 and total["p95"] <= 2000
        color = "green" if target_ok else "red"
        console.print(f"\n[{color}]Target p50 ≤ 1200ms / p95 ≤ 2000ms: {'MET' if target_ok else 'NOT MET'}[/{color}]\n")


@voice_app.command(name="latency-model")
def voice_latency_model(
    calls: int = typer.Option(20, "--calls", "-n", help="Number of most recent calls to analyze"),
    sort: str = typer.Option("p50", "--sort", help="Row order: 'p50' (fastest total first) or 'name'"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Latency per LLM model — p50/p95 of each stage grouped by the model that served the turn.

    Reads data/logs/voice_latency.jsonl (the ``turn_model`` stamp on each turn);
    turns without a model stamp are reported under 'unknown'."""
    import json as _json

    from pincer.config import get_settings_relaxed
    from pincer.voice.latency_report import MODEL_STAGES, build_model_report, read_turn_records

    if sort not in ("p50", "name"):
        console.print(f"[red]--sort must be 'p50' or 'name', got {sort!r}[/red]")
        raise typer.Exit(2)

    settings = get_settings_relaxed()
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    records = read_turn_records(path, last_calls=calls)
    if not records:
        console.print(f"[yellow]No turn records in {path} — run some calls first.[/yellow]")
        raise typer.Exit(1)

    rows = build_model_report(records, sort=sort)
    if output_json:
        console.print(_json.dumps(rows, indent=2))
        return

    from rich.table import Table

    total_turns = sum(row["turns"] for row in rows)
    total_calls = len({str(r.get("call_sid")) for r in records})
    console.print(
        f"\n[bold]Voice latency by model[/bold] — {total_turns} turn(s) across {total_calls} call(s), "
        f"{len(rows)} model(s)\n"
    )
    stage_labels = {
        "total_ms": "total",
        "llm_first_token_ms": "first token",
        "first_dispatch_ms": "first dispatch",
        "llm_done_ms": "llm done",
    }
    table = Table(show_header=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Turns", justify="right")
    table.add_column("Calls", justify="right")
    table.add_column("Err", justify="right")
    for stage in MODEL_STAGES:
        table.add_column(f"{stage_labels.get(stage, stage)}\np50 / p95", justify="right")
    for row in rows:
        cells = [row["model"], str(row["turns"]), str(row["calls"]), str(row["errors"])]
        for stage in MODEL_STAGES:
            stats = row["stages"].get(stage)
            cells.append(f"{stats['p50']:.0f} / {stats['p95']:.0f}" if stats else "-")
        table.add_row(*cells)
    console.print(table)
    console.print(
        "\n[dim]Times in ms, fastest total p50 first. "
        "'unknown' = turns recorded before the model stamp or that failed pre-LLM.[/dim]\n"
    )


@voice_app.command(name="list")
def voice_list() -> None:
    """List ElevenLabs voices (ID, name, category, languages) — find your cloned voice's ID here."""
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.voice.voices import VoiceLookupError, configured_voice_ids, list_voices

    settings = get_settings_relaxed()
    api_key = settings.elevenlabs_api_key.get_secret_value()
    if not api_key:
        console.print("[red]PINCER_ELEVENLABS_API_KEY not set.[/red]")
        raise typer.Exit(1)

    try:
        voices = list_voices(api_key)
    except VoiceLookupError as e:
        console.print(f"[red]Could not list voices: {e}[/red]")
        raise typer.Exit(1) from e

    configured = configured_voice_ids(settings)
    table = Table(title=f"ElevenLabs Voices ({len(voices)})")
    table.add_column("Voice ID", style="cyan")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Languages")
    table.add_column("")
    for voice in voices:
        table.add_row(
            voice.voice_id,
            voice.name,
            voice.category,
            ", ".join(voice.languages) or "-",
            "[green]configured[/green]" if voice.voice_id in configured else "",
        )
    console.print(table)
    console.print(
        "\n[dim]Set PINCER_ELEVENLABS_VOICE_ID (or _EN / _DE) to a Voice ID above, "
        "then judge it at telephony quality with `pincer voice test`.[/dim]"
    )


@voice_app.command(name="test")
def voice_test(
    voice_id: str = typer.Option("", "--voice-id", help="Voice ID to test (default: the configured voice)"),
    language: str = typer.Option("", "--language", help="Call language the voice is resolved for (en/de)"),
    text: str = typer.Option("", "--text", help="Custom sample text"),
) -> None:
    """Synthesize a sample to ~/.pincer/voice_test.wav (+ 8kHz mu-law variant at telephony quality)."""
    from pathlib import Path

    from pincer.config import get_settings_relaxed
    from pincer.voice.language import elevenlabs_model_for, resolve_call_language, voice_for
    from pincer.voice.voices import VoiceLookupError, synthesize_sample, ulaw_to_wav

    settings = get_settings_relaxed()
    api_key = settings.elevenlabs_api_key.get_secret_value()
    if not api_key:
        console.print("[red]PINCER_ELEVENLABS_API_KEY not set.[/red]")
        raise typer.Exit(1)

    lang = resolve_call_language(settings, language)
    resolved_voice = voice_id.strip() or voice_for(settings, lang)
    model = elevenlabs_model_for(settings, lang)
    samples = {
        "en": "Hello! I'm your personal assistant. This is how I sound on the phone.",
        "de": "Guten Tag! Ich bin Ihr persönlicher Assistent. So klinge ich am Telefon.",
        "uk": "Вітаю! Я ваш особистий асистент. Ось так я звучу по телефону.",
    }
    sample_text = text.strip() or samples.get(lang, samples["en"])

    out_dir = Path.home() / ".pincer"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "voice_test.wav"
    ulaw_path = out_dir / "voice_test_ulaw.wav"

    console.print(f"Synthesizing with voice [cyan]{resolved_voice}[/cyan], model {model}, language {lang}...")
    try:
        wav_path.write_bytes(synthesize_sample(api_key, resolved_voice, sample_text, model, "wav_16000"))
        ulaw_raw = synthesize_sample(api_key, resolved_voice, sample_text, model, "ulaw_8000")
        ulaw_path.write_bytes(ulaw_to_wav(ulaw_raw))
    except VoiceLookupError as e:
        console.print(f"[red]Synthesis failed: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]Wrote {wav_path}[/green] (16 kHz)")
    console.print(f"[green]Wrote {ulaw_path}[/green] (8 kHz mu-law — how callers will hear it)")
    console.print("[dim]Some voices sound very different at telephony quality — judge the mu-law file.[/dim]")


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

    settings = get_settings_relaxed()
    logger = AuditLogger(db_path=settings.db_path)
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

memory_app = typer.Typer(
    name="memory", help="Manage local sqlite based conversation memory. MCP servers are not supported yet!"
)
app.add_typer(memory_app, name="memory")


@memory_app.command(name="search")
def memory_search(query: str = typer.Argument(help="Search query")) -> None:
    """Search conversation memory."""
    asyncio.run(_memory_search(query))


async def _memory_search(query: str) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
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
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    total = await store.count()
    assert store._db is not None
    async with store._db.execute("SELECT COUNT(DISTINCT user_id) FROM memories") as cur:
        row = await cur.fetchone()
        users = row[0] if row else 0
    async with store._db.execute("SELECT category, COUNT(*) FROM memories GROUP BY category") as cur:
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
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()
    await store.delete_user_memories(user_id)
    console.print(f"[green]Cleared memories for {user_id}[/green]")
    await store.close()


@memory_app.command(name="list")
def memory_list(
    user_id: str = typer.Option(None, "--user", help="Filter by user ID"),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tag filter (OR logic)"),
    limit: int = typer.Option(20, "--limit", help="Records per page"),
    offset: int = typer.Option(0, "--offset", help="Pagination offset"),
) -> None:
    """List memories, newest first, with optional user, tag, and pagination filters."""
    asyncio.run(_memory_list(user_id, tags, limit, offset))


async def _memory_list(user_id: str | None, tags_str: str | None, limit: int, offset: int) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    tag_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None
    memories = await store.list_memories(user_id=user_id, limit=limit, offset=offset, tags=tag_list)

    if not memories:
        console.print("[dim]No memories found.[/dim]")
    else:
        for mem in memories:
            tag_str = f" {mem.tags}" if mem.tags else ""
            console.print(f"  [{mem.category}]{tag_str} {mem.content[:200]}")
        console.print(f"\n[dim]Showing {len(memories)} records (offset={offset})[/dim]")

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
    from pathlib import Path as _P

    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    memories = await store.list_memories(user_id=user_id, limit=100_000)
    records = [{"content": m.content, "category": m.category, "created_at": m.created_at} for m in memories]

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

    settings = get_settings_relaxed()
    async with _aiosqlite.connect(str(settings.db_path)) as db:
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


# ── DB subcommands ─────────────────────────────

db_app = typer.Typer(name="db", help="Manage the Pincer database schema")
app.add_typer(db_app, name="db")


@db_app.command(name="upgrade")
def db_upgrade() -> None:
    """Apply any pending schema migrations.

    Runs automatically whenever the app starts — this is for manual/ops use
    (e.g. applying migrations ahead of a deploy).
    """
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.upgrade(build_config(settings.db_path), "head")
    console.print(f"[green]Database at head: {settings.db_path}[/green]")


@db_app.command(name="current")
def db_current() -> None:
    """Show the currently applied migration revision."""
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.current(build_config(settings.db_path), verbose=True)


@db_app.command(name="history")
def db_history() -> None:
    """Show the full migration history."""
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.history(build_config(settings.db_path), verbose=True)


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
            f"{s.timeout}s",
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
