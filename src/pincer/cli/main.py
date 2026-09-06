"""Pincer CLI — the main entry point.

Usage:
    pincer run          Start the agent
    pincer run tasks    Start only the background task worker (requires PINCER_TASK_BROKER=redis)
    pincer config       Show current configuration
    pincer cost         Show today's spend
"""

from __future__ import annotations

from async_typer import AsyncTyper

from pincer.cli import (
    audit,
    chat,
    config,
    cost,
    db,
    doctor,
    google,
    init,
    mcp,
    memory,
    schedule,
    signal,
    slack,
    whatsapp,
)
from pincer.cli import run as run_cmd

app = AsyncTyper(
    name="pincer",
    help="Pincer — Your personal AI agent",
    no_args_is_help=True,
)

app.command(name="run")(run_cmd.run)
app.command()(config.config)
app.command()(cost.cost)
app.command(name="init")(init.init)
app.command()(doctor.doctor)
app.command()(chat.chat)

app.add_typer(google.google_app, name="google")
app.add_typer(signal.signal_app, name="signal")
app.add_typer(slack.slack_app, name="slack")
app.add_typer(whatsapp.whatsapp_app, name="whatsapp")
app.add_typer(audit.audit_app, name="audit")
app.add_typer(memory.memory_app, name="memory")
app.add_typer(schedule.schedule_app, name="schedule")
app.add_typer(db.db_app, name="db")
app.add_typer(mcp.mcp_app, name="mcp")
