"""
Pomodoro MCP Server
A Model Context Protocol server for managing Pomodoro sessions.
"""

import argparse
import json
import uuid
from datetime import UTC, datetime

import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import Context, FastMCP
from pydantic import Field
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from state import BreakType, PomodoroStore, Session, SessionStatus

mcp = FastMCP(
    name="pomodoro_mcp",
    instructions=(
        ""
    ),
)
store = PomodoroStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def _session_to_dict(s: Session) -> dict:
    now = datetime.now(UTC)
    remaining: int | None = None
    elapsed: int | None = None

    if s.status == SessionStatus.RUNNING and s.started_at:
        elapsed = int((now - s.started_at).total_seconds())
        remaining = max(0, s.duration_seconds - elapsed)
    elif s.status == SessionStatus.PAUSED and s.paused_at and s.started_at:
        elapsed = int((s.paused_at - s.started_at).total_seconds() - s.paused_duration)
        remaining = max(0, s.duration_seconds - elapsed)

    return {
        "id": s.id,
        "type": s.session_type,
        "description": s.description,
        "tags": s.tags,
        "status": s.status,
        "duration_minutes": s.duration_seconds // 60,
        "elapsed_formatted": _format_duration(elapsed) if elapsed is not None else None,
        "remaining_formatted": _format_duration(remaining) if remaining is not None else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        "cancelled_at": s.cancelled_at.isoformat() if s.cancelled_at else None,
    }

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "pomodoro-mcp",
            "version": "0.1.0",
            "transport": "http",
        }
    )

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
async def pomodoro_start(
    description: str = Field(min_length=1, max_length=200,
        description="What you are working on (e.g. 'Write unit tests for auth "
            " module')"),
    duration_minutes: int = Field(default=25, ge=1, le=90,
        description="Focus session length in minutes (1–90). Default is 25."),
    tags: list[str] = Field(default_factory=list,
        description="Optional labels such as ['work', 'deep-focus']"),
    ctx: Context = None,
) -> str:
    """Start a new Pomodoro focus session.

    Creates and immediately starts a timed work session. Only one session
    can be active at a time; finish or cancel the current one first.
    """
    if store.active_session:
        return json.dumps({
            "error": "A session is already active.",
            "active_session": _session_to_dict(store.active_session),
            "hint": "Use pomodoro_finish, pomodoro_cancel, or pomodoro_pause first.",
        }, indent=2)

    session = Session(
        id=str(uuid.uuid4())[:8],
        session_type="pomodoro",
        description=description,
        tags=tags,
        duration_seconds=duration_minutes * 60,
        status=SessionStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    store.set_active(session)
    if ctx:
        await ctx.info(f"Pomodoro started: {description}")

    return json.dumps({
        "message": f"🍅 Pomodoro started! Focus for {duration_minutes} minutes.",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_status(ctx: Context = None) -> str:
    """Get the status of the currently active Pomodoro or break session.

    Returns time elapsed, time remaining, and session metadata.
    Returns a message if no session is active.
    """
    if not store.active_session:
        return json.dumps({
            "status": "idle",
            "message": "No active session. Use pomodoro_start to begin.",
            "today": store.daily_stats(),
        }, indent=2)

    return json.dumps({
        "status": "active",
        "session": _session_to_dict(store.active_session),
        "today": store.daily_stats(),
    }, indent=2)


@mcp.tool
async def pomodoro_pause(ctx: Context = None) -> str:
    """Pause the currently running Pomodoro session.

    Freezes the timer; resume with pomodoro_resume.
    """
    session = store.active_session

    if not session:
        return json.dumps({"error": "No active session to pause."}, indent=2)

    if session.status == SessionStatus.PAUSED:
        return json.dumps({"error": "Session is already paused.", "session": _session_to_dict(session)}, indent=2)

    session.status = SessionStatus.PAUSED
    session.paused_at = datetime.now(UTC)
    if ctx:
        await ctx.info("Session paused")

    return json.dumps({
        "message": "⏸ Session paused.",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_resume(ctx: Context = None) -> str:
    """Resume a paused Pomodoro session.

    Continues the timer from where it was paused.
    """
    session = store.active_session

    if not session:
        return json.dumps({"error": "No active session."}, indent=2)

    if session.status != SessionStatus.PAUSED:
        return json.dumps({"error": "Session is not paused.", "session": _session_to_dict(session)}, indent=2)

    now = datetime.now(UTC)
    if session.paused_at:
        session.paused_duration += int((now - session.paused_at).total_seconds())
        session.paused_at = None

    session.status = SessionStatus.RUNNING
    if ctx:
        await ctx.info("Session resumed")

    return json.dumps({
        "message": "▶ Session resumed.",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_finish(ctx: Context = None) -> str:
    """Mark the active session as successfully completed.

    Records it in history and clears the active slot.
    Suggests whether to take a short or long break next.
    """
    session = store.active_session

    if not session:
        return json.dumps({"error": "No active session to finish."}, indent=2)

    session.status = SessionStatus.COMPLETED
    session.completed_at = datetime.now(UTC)
    store.complete_active()

    stats = store.daily_stats()
    pomodoros_done = stats["pomodoros_completed"]
    recommendation = (
        "Take a long break (15 min) — you've earned it! 🎉"
        if pomodoros_done % 4 == 0
        else "Take a short break (5 min) ☕"
    )

    return json.dumps({
        "message": f"✅ Session complete! {recommendation}",
        "session": _session_to_dict(session),
        "today": stats,
        "next_step": "Use pomodoro_break to start a break.",
    }, indent=2)


@mcp.tool
async def pomodoro_cancel(ctx: Context = None) -> str:
    """Cancel and discard the active session without recording it as complete.

    Use when you were interrupted and don't want to count this session.
    """
    session = store.active_session

    if not session:
        return json.dumps({"error": "No active session to cancel."}, indent=2)

    session.status = SessionStatus.CANCELLED
    session.cancelled_at = datetime.now(UTC)
    store.complete_active()
    if ctx:
        await ctx.info("Session cancelled")

    return json.dumps({
        "message": "🚫 Session cancelled.",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_break(
    break_type: BreakType = Field(default=BreakType.SHORT,
        description="'short' (5 min) or 'long' (15 min). Custom overrides "
            "duration_minutes."),
    duration_minutes: int | None = Field(default=None, ge=1, le=60,
        description="Override the default break length (1–60 minutes)"),
    ctx: Context = None,
) -> str:
    """Start a short or long break session.

    Short breaks are 5 minutes; long breaks are 15 minutes.
    Use duration_minutes to override. A break counts as an active session.
    """
    if store.active_session:
        return json.dumps({
            "error": "Finish or cancel the current session before starting a break.",
            "active_session": _session_to_dict(store.active_session),
        }, indent=2)

    defaults = {BreakType.SHORT: 5, BreakType.LONG: 15}
    duration = duration_minutes or defaults[break_type]
    session_type = f"{break_type}_break"

    session = Session(
        id=str(uuid.uuid4())[:8],
        session_type=session_type,
        description=f"{'Short' if break_type == BreakType.SHORT else 'Long'} break",
        tags=[],
        duration_seconds=duration * 60,
        status=SessionStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    store.set_active(session)

    emoji = "☕" if break_type == BreakType.SHORT else "🌿"
    return json.dumps({
        "message": f"{emoji} Break started for {duration} minutes. Relax!",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_amend(
    description: str | None = Field(default=None, min_length=1, max_length=200,
        description="New task description"),
    tags: list[str] | None = Field(default=None,
        description="Replace the tag list"),
    ctx: Context = None,
) -> str:
    """Update the description or tags of the currently active session.

    Useful when the task changed mid-session.
    """
    session = store.active_session

    if not session:
        return json.dumps({"error": "No active session to amend."}, indent=2)

    if description:
        session.description = description
    if tags is not None:
        session.tags = tags

    return json.dumps({
        "message": "✏️ Session updated.",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_repeat(ctx: Context = None) -> str:
    """Start a new Pomodoro using the same settings as the last completed one.

    Copies the description, duration, and tags from the most recent
    completed Pomodoro session.
    """
    if store.active_session:
        return json.dumps({
            "error": "Finish or cancel the current session first.",
            "active_session": _session_to_dict(store.active_session),
        }, indent=2)

    last = store.last_pomodoro()
    if not last:
        return json.dumps({"error": "No previous Pomodoro found to repeat."}, indent=2)

    session = Session(
        id=str(uuid.uuid4())[:8],
        session_type="pomodoro",
        description=last.description,
        tags=list(last.tags),
        duration_seconds=last.duration_seconds,
        status=SessionStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    store.set_active(session)

    return json.dumps({
        "message": f"🔁 Repeating: {last.description}",
        "session": _session_to_dict(session),
    }, indent=2)


@mcp.tool
async def pomodoro_history(
    limit: int = Field(default=10, ge=1, le=50,
        description="Number of past sessions to return (1–50)"),
    session_type: str | None = Field(default=None,
        description="Filter by type: 'pomodoro, 'short_break' or 'long_break'"),
    tag: str | None = Field(default=None,
        description="Filter sessions that include this tag"),
    ctx: Context = None,
) -> str:
    """List past Pomodoro and break sessions with optional filters."""
    sessions = store.history(limit=limit, session_type=session_type, tag=tag)

    return json.dumps({
        "count": len(sessions),
        "sessions": [_session_to_dict(s) for s in sessions],
        "today": store.daily_stats(),
    }, indent=2)


@mcp.tool
async def pomodoro_settings(ctx: Context = None) -> str:
    """Return the current default settings and today's progress summary."""
    return json.dumps({
        "defaults": {
            "pomodoro_minutes": 25,
            "short_break_minutes": 5,
            "long_break_minutes": 15,
            "long_break_interval": 4,
        },
        "today": store.daily_stats(),
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    parser = argparse.ArgumentParser(description="Stock & crypto price MCP server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'http' (streamable HTTP, default) or 'stdio'",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        print(f"Starting stock_price_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
