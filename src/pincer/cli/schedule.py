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
