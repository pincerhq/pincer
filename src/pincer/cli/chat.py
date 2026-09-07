"""`pincer chat` — interactive CLI chat for testing the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.cli._shared import _create_memory_backend, _setup_logging, console

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend
    from pincer.memory.summarizer import Summarizer


async def chat() -> None:
    """Interactive CLI chat — test the agent without messaging apps."""
    await _chat_loop()


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
