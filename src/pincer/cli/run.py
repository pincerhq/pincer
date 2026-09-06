"""`pincer run` — start the full agent, or a standalone component of it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import typer

from pincer.cli._attachments import _IMAGE_EXTENSIONS, _format_image_attachment, _format_pdf_attachment
from pincer.cli._shared import _create_memory_backend, _port_in_use, _print_voice_webhook_urls, _setup_logging, console

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


class RunComponent(StrEnum):
    """A single component of the full agent that `pincer run` can start standalone."""

    TASKS = "tasks"


async def run(
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
        await _run_tasks_worker(settings)
        return

    console.print(f"[bold green]{settings.agent_name} starting...[/bold green]")
    console.print(f"   Provider: {settings.default_provider}")
    console.print(f"   Model: {settings.default_model}")
    console.print(f"   Budget: ${settings.daily_budget_usd:.2f}/day")
    console.print(f"   Data: {settings.data_dir}")
    console.print(f"   Skills dir: {settings.skills_dir}")
    console.print()

    await _run_agent(settings)


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
        )

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

        tg = TelegramChannel(settings, identity=identity)
        tg.set_stream_agent(agent)
        await tg.start(on_message)
        channel_map[tg.name] = tg
        console.print("[green]Telegram connected (streaming enabled)[/green]")
    if tg:
        router.register(ChannelType.TELEGRAM, tg)

    if settings.whatsapp_enabled:
        try:
            from pincer.channels.whatsapp import WhatsAppChannel

            wa = WhatsAppChannel(settings, identity=identity)
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
        try:
            from pincer.channels.phone_calls import VoiceChannel
            from pincer.voice.engine import get_voice_engine
            from pincer.voice.outbound import make_phone_call
            from pincer.voice.twiml_server import init_voice_routes

            voice_engine = get_voice_engine(settings)
            vc = VoiceChannel(settings)
            vc.set_engine(voice_engine)
            await vc.start(on_message)
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

                sig = SignalChannel(settings, identity=identity)
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
    # See pincer.tasks.delivery.
    from pincer.tasks.delivery import ResultEmitter, ResultRelay, create_delivery_backend

    delivery_backend = create_delivery_backend(settings)
    deliverer = ResultEmitter(delivery_backend)

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
