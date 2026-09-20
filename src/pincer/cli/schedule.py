"""`pincer schedule` — manage scheduled tasks."""

from __future__ import annotations

from async_typer import AsyncTyper

from pincer.cli._shared import console

schedule_app = AsyncTyper(name="schedule", help="Manage scheduled tasks")


@schedule_app.command(name="list")
async def schedule_list() -> None:
    """List all scheduled tasks."""
    await _schedule_list()


async def _schedule_list() -> None:
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.services.scheduler import ScheduleService

    settings = get_settings_relaxed()
    try:
        service = await ScheduleService.for_path(settings.db_path)
        schedules = sorted(await service.list_all(), key=lambda s: s["name"])
    except Exception:
        console.print("[dim]No scheduled tasks (table not created yet).[/dim]")
        return
    rows = [(s["name"], s["cron_expr"], s["pincer_user_id"], s["timezone"], s["enabled"]) for s in schedules]

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
