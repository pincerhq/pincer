"""Pincer CLI — the main entry point.

Usage:
    pincer run          Start the agent (or attach to the background one, if it is running)
    pincer run -d       Start the agent in the background; returns once its channels are up
    pincer run tasks    Start only the background task worker (requires PINCER_TASK_BROKER=redis)
    pincer logs         Print the background agent's log (-n N: last N lines, -f: follow)
    pincer status       Show whether the background agent is running
    pincer stop         Stop the background agent gracefully (--force kills it)
    pincer restart      Stop the background agent and start a new one from here
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
    daemon,
    db,
    doctor,
    google,
    identity,
    init,
    mcp,
    memory,
    pilot,
    schedule,
    signal,
    skills,
    slack,
    telephony,
    voice,
    whatsapp,
)
from pincer.cli import run as run_cmd

app = AsyncTyper(
    name="pincer",
    help="Pincer — Your personal AI agent",
    no_args_is_help=True,
)

app.command(name="run")(run_cmd.run)
app.command()(daemon.stop)
app.command()(daemon.restart)
app.command()(daemon.logs)
app.command()(daemon.status)
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
app.add_typer(skills.skills_app, name="skills")
app.add_typer(identity.identity_app, name="identity")
app.add_typer(db.db_app, name="db")
app.add_typer(mcp.mcp_app, name="mcp")
app.add_typer(voice.voice_app, name="voice")
app.add_typer(telephony.telephony_app, name="telephony")
app.add_typer(pilot.pilot_app, name="pilot")
