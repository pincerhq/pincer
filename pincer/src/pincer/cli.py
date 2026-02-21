"""
Pincer CLI — the main entry point.

Usage:
    pincer run          Start the agent
    pincer config       Show current configuration
    pincer cost         Show today's spend
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    from pincer.channels.base import IncomingMessage

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


async def _run_agent(settings: Settings) -> None:  # noqa: F821
    from pincer.core.agent import Agent
    from pincer.core.session import SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.memory.store import MemoryStore
    from pincer.memory.summarizer import Summarizer
    from pincer.tools.builtin.files import file_list, file_read, file_write
    from pincer.tools.builtin.shell import shell_exec
    from pincer.tools.builtin.web_search import web_search
    from pincer.tools.registry import ToolRegistry

    # Initialize components
    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()

    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    # Initialize memory system
    memory_store: MemoryStore | None = None
    summarizer: Summarizer | None = None

    # Create LLM provider
    if settings.default_provider.value == "anthropic":
        from pincer.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(settings)
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
            description=(
                "Take a screenshot of a web page. "
                "Use when the user wants to see what a page looks like."
            ),
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
            "Common libraries available: pandas, numpy, matplotlib."
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

    # Message handler bridge
    async def on_message(incoming: IncomingMessage) -> str:
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
                ".txt", ".py", ".js", ".ts", ".json", ".csv", ".md",
                ".log", ".xml", ".html", ".css", ".yaml", ".yml",
                ".toml", ".ini", ".cfg", ".sh", ".bash", ".sql",
                ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb",
                ".php", ".swift", ".kt", ".env", ".gitignore",
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
                            f"[File: {filename}] saved to {abs_path} "
                            f"(binary, {len(raw_bytes)} bytes, {mime})"
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
                            f"[File: {filename} — {len(pages)} pages, saved to {abs_path}]\n"
                            f"```\n{content}\n```"
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

    # Start channels
    channels = []
    if settings.telegram_bot_token.get_secret_value():
        from pincer.channels.telegram import TelegramChannel

        tg = TelegramChannel(settings)
        tg.set_stream_agent(agent)
        await tg.start(on_message)
        channels.append(tg)
        console.print("[green]Telegram connected (streaming enabled)[/green]")

    if not channels:
        console.print(
            "[yellow]No channels configured. Set PINCER_TELEGRAM_BOT_TOKEN.[/yellow]"
        )
        return

    console.print(
        f"\n[bold green]{settings.agent_name} is running![/bold green] Press Ctrl+C to stop.\n"
    )

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/yellow]")
    finally:
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
        console.print("[green]Shutdown complete[/green]")


@app.command()
def config() -> None:
    """Show current configuration."""
    from pincer.config import get_settings

    try:
        s = get_settings()
        console.print("[bold]Pincer Configuration[/bold]\n")
        console.print(f"  Provider:     {s.default_provider.value}")
        console.print(f"  Model:        {s.default_model}")
        console.print(
            f"  Anthropic:    {'set' if s.anthropic_api_key.get_secret_value() else 'not set'}"
        )
        console.print(
            f"  OpenAI:       {'set' if s.openai_api_key.get_secret_value() else 'not set'}"
        )
        console.print(
            f"  Telegram:     {'set' if s.telegram_bot_token.get_secret_value() else 'not set'}"
        )
        console.print(f"  Budget:       ${s.daily_budget_usd:.2f}/day")
        console.print(f"  Data dir:     {s.data_dir}")
        console.print(f"  Shell:        {'enabled' if s.shell_enabled else 'disabled'}")
        console.print(f"  Log level:    {s.log_level.value}")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@app.command()
def cost() -> None:
    """Show today's API cost."""
    asyncio.run(_show_cost())


async def _show_cost() -> None:
    from pincer.config import get_settings
    from pincer.llm.cost_tracker import CostTracker

    s = get_settings()
    tracker = CostTracker(s.db_path, s.daily_budget_usd)
    await tracker.initialize()
    today = await tracker.get_today_spend()
    summary = await tracker.get_summary()
    await tracker.close()
    console.print(f"Today:  ${today:.4f} / ${s.daily_budget_usd:.2f}")
    console.print(f"Total:  ${summary.total_usd:.4f} ({summary.total_calls} calls)")
