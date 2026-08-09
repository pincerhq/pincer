"""
Scheduling tools — create/list/remove/toggle recurring background jobs.

Exposes `CronScheduler` (src/pincer/scheduler/cron.py) CRUD to the LLM so a
chat directive like "send me a digest every day at 8am" can actually create
a durable schedule. Scheduled prompts run later via `Agent.run_headless`
(see `ProactiveAgent.run_custom_action`) with no conversation context or
memory carried over — prompts must be fully self-contained.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from pincer.channels.base import ChannelType
from pincer.config import get_settings
from pincer.scheduler.cron import CronScheduler

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pincer.tools.registry import ToolRegistry


def _is_unresolved_identity(pincer_user_id: str) -> bool:
    """True if `pincer_user_id` looks like IdentityMiddleware's synthetic fallback.

    When PINCER_IDENTITY_MAP is configured and a sender isn't listed in it,
    the middleware hands downstream code a "{channel}:{native_id}" pseudo-id
    (see channels/middleware.py) purely so nothing sees an empty string —
    it's never persisted to identity_meta/channel_identities, so a schedule
    stored under it can never be delivered later. This is a heuristic (a
    genuinely configured canonical name could coincidentally collide with
    this pattern), matching the same prefix check cli.py already uses to
    detect this case for the live-message path.
    """
    return pincer_user_id.startswith(tuple(f"{c.value}:" for c in ChannelType))


async def _resolve_schedule_id(
    scheduler: CronScheduler,
    pincer_user_id: str,
    name: str,
    schedule_id: int | None,
) -> int:
    """Resolve a schedule by explicit id or by name, scoped to the calling user."""
    if schedule_id is not None:
        return schedule_id

    existing = await scheduler.list_schedules(pincer_user_id)
    matches = [s for s in existing if s["name"].lower() == name.lower()]
    if not matches:
        raise ValueError(f"No schedule named '{name}' found. Use schedule_list to see your schedules.")
    if len(matches) > 1:
        ids = ", ".join(str(m["id"]) for m in matches)
        raise ValueError(f"Multiple schedules named '{name}' found (ids: {ids}). Pass schedule_id to disambiguate.")
    return int(matches[0]["id"])


def make_schedule_create_handler(tool_registry: ToolRegistry) -> Callable[..., Awaitable[str]]:
    """Return a `schedule_create` handler closed over the live ToolRegistry.

    The registry reference is only needed to validate `allowed_tools`
    against real tool names at creation time.
    """

    async def schedule_create(
        name: str,
        prompt: str,
        cron_expr: str = "",
        run_in_minutes: int | None = None,
        tz: str = "",
        channel: str = "",
        allowed_tools: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Create a schedule that runs `prompt` through the agent later.

        prompt runs later with NO conversation context or memory — it must be
        fully self-contained (e.g. "search the web for X and summarize", not
        "the thing we discussed"). Pass exactly one of cron_expr (recurring,
        e.g. "0 8 * * *" for daily 8am) or run_in_minutes (a one-time run —
        computed from the real current time, not left to guesswork).
        """
        ctx = context or {}
        pincer_user_id = ctx.get("pincer_user_id", "")
        if not pincer_user_id:
            return "Error: no user identity available."
        if _is_unresolved_identity(pincer_user_id):
            return (
                "Error: your identity isn't fully linked yet, so a scheduled job can't be "
                "reliably delivered to you later. Ask the bot administrator to add your "
                "account, then try again."
            )

        channel_name = channel or ctx.get("channel_name", "")
        valid_channels = {c.value for c in ChannelType}
        if channel_name not in valid_channels:
            return f"Error: invalid channel '{channel_name}'. Valid channels: {', '.join(sorted(valid_channels))}"

        settings = get_settings()
        resolved_tz = tz or settings.timezone
        try:
            tzinfo = ZoneInfo(resolved_tz)
        except Exception:
            return f"Error: invalid timezone: {resolved_tz}"

        if bool(cron_expr) == bool(run_in_minutes):
            return "Error: pass exactly one of cron_expr (recurring) or run_in_minutes (one-time)."

        is_one_time = run_in_minutes is not None
        if is_one_time:
            if run_in_minutes < 1:  # type: ignore[operator]
                return "Error: run_in_minutes must be at least 1."
            target = datetime.now(tzinfo) + timedelta(minutes=run_in_minutes)  # type: ignore[arg-type]
            cron_expr = f"{target.minute} {target.hour} {target.day} {target.month} *"

        if not croniter.is_valid(cron_expr):
            return f"Error: Invalid cron expression: {cron_expr}"

        # Guard against overly-frequent recurring schedules (e.g. an accidental
        # '* * * * *') — a one-time schedule's pinned date naturally clears this.
        it = croniter(cron_expr, datetime.now(tzinfo))
        first_run = it.get_next(datetime)
        second_run = it.get_next(datetime)
        min_gap = timedelta(minutes=settings.min_schedule_interval_minutes)
        if second_run - first_run < min_gap:
            return (
                f"Error: this schedule would fire more often than every "
                f"{settings.min_schedule_interval_minutes} minutes ({cron_expr}). "
                "Use a less frequent cron expression, or run_in_minutes for a one-time action."
            )

        if allowed_tools is not None:
            valid_tools = {s["name"] for s in tool_registry.get_schemas()}
            bad_tools = [t for t in allowed_tools if t not in valid_tools]
            if bad_tools:
                return f"Error: unknown tool(s) in allowed_tools: {', '.join(bad_tools)}"

        scheduler = CronScheduler(settings.db_path)

        existing = await scheduler.list_schedules(pincer_user_id)
        if any(s["name"].lower() == name.lower() for s in existing):
            return f"Error: a schedule named '{name}' already exists. Remove it first or choose a different name."
        if len(existing) >= settings.max_schedules_per_user:
            return (
                f"Error: you already have {len(existing)} scheduled jobs "
                f"(limit {settings.max_schedules_per_user}). Remove one first."
            )

        action: dict[str, Any] = {"type": "custom", "prompt": prompt}
        if allowed_tools is not None:
            action["allowed_tools"] = allowed_tools

        try:
            sid = await scheduler.add(
                name=name,
                cron_expr=cron_expr,
                action=action,
                pincer_user_id=pincer_user_id,
                tz=resolved_tz,
                channel=channel_name,
            )
        except ValueError as e:
            return f"Error: {e}"

        schedule = await scheduler.get(sid)
        next_run = schedule.next_run_at if schedule else "unknown"
        if is_one_time:
            return f"Scheduled '{name}': runs once at {next_run}. Delivers to {channel_name}."
        return (
            f"Scheduled '{name}': runs '{cron_expr}' ({resolved_tz}), next run {next_run}. Delivers to {channel_name}."
        )

    return schedule_create


async def schedule_list(context: dict[str, Any] | None = None) -> str:
    """List your scheduled jobs."""
    ctx = context or {}
    pincer_user_id = ctx.get("pincer_user_id", "")
    if not pincer_user_id:
        return "Error: no user identity available."

    scheduler = CronScheduler(get_settings().db_path)
    schedules = await scheduler.list_schedules(pincer_user_id)
    if not schedules:
        return "You have no scheduled jobs."

    lines = []
    for i, s in enumerate(schedules, 1):
        action = json.loads(s["action"]) if isinstance(s["action"], str) else s["action"]
        allowed_tools = action.get("allowed_tools")
        tools_str = ", ".join(allowed_tools) if allowed_tools else "all"
        status = "enabled" if s["enabled"] else "disabled"
        lines.append(
            f"{i}. {s['name']} — cron '{s['cron_expr']}' ({s['timezone']}), "
            f"channel={s['channel']}, {status}, tools={tools_str}, next run={s['next_run_at']}"
        )
    return "\n".join(lines)


async def schedule_remove(
    name: str,
    schedule_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Remove a scheduled job by name (or schedule_id if names collide)."""
    ctx = context or {}
    pincer_user_id = ctx.get("pincer_user_id", "")
    if not pincer_user_id:
        return "Error: no user identity available."

    scheduler = CronScheduler(get_settings().db_path)
    try:
        sid = await _resolve_schedule_id(scheduler, pincer_user_id, name, schedule_id)
    except ValueError as e:
        return f"Error: {e}"

    removed = await scheduler.remove(sid)
    return f"Removed schedule '{name}'." if removed else f"Error: schedule '{name}' not found."


async def schedule_toggle(
    name: str,
    enabled: bool,
    schedule_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Enable or disable a scheduled job by name (or schedule_id if names collide)."""
    ctx = context or {}
    pincer_user_id = ctx.get("pincer_user_id", "")
    if not pincer_user_id:
        return "Error: no user identity available."

    scheduler = CronScheduler(get_settings().db_path)
    try:
        sid = await _resolve_schedule_id(scheduler, pincer_user_id, name, schedule_id)
    except ValueError as e:
        return f"Error: {e}"

    toggled = await scheduler.toggle(sid, enabled)
    state = "enabled" if enabled else "disabled"
    return f"Schedule '{name}' {state}." if toggled else f"Error: schedule '{name}' not found."
