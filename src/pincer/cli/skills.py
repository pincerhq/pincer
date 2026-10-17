"""`pincer skills` — manage the SKILL.md skill index."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

skills_app = AsyncTyper(name="skills", help="Manage SKILL.md skills (bundled and user roots)")


@skills_app.command(name="rebuild")
def skills_rebuild(
    keep_cache: bool = typer.Option(
        False,
        "--keep-cache",
        help="Keep the cached copies of remote skill files (default: clear them and re-fetch from the bucket)",
    ),
) -> None:
    """Rescan the skill roots and invalidate cached skill data.

    Clears cached remote files, rescans, and tells a running `pincer run` to reload.
    """
    from pathlib import Path

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.services.skills import SkillService

    settings = get_settings_relaxed()
    try:
        service = SkillService.from_settings(settings, require_usable_root=True)
        index = service.rebuild(clear_cache=not keep_cache)
    except Exception as e:
        console.print(f"[red]Could not rebuild the skill index: {e}[/red]")
        raise typer.Exit(code=1) from e

    entries = index.all_skills()
    table = Table(show_header=True)
    table.add_column("Skill", style="bold")
    table.add_column("Root")
    table.add_column("Directory", style="dim", overflow="fold")
    for entry in entries:
        table.add_row(entry.name, entry.skill.source_root, str(entry.skill.path))
    console.print(table)

    user_root = service.user_dir
    if user_root is None:
        root_note = "User root: unavailable (see the log)."
    elif isinstance(user_root, Path):
        root_note = f"User root: {user_root}."
    else:
        root_note = f"User root: {user_root} (remote file cache {'kept' if keep_cache else 'cleared'})."
    console.print(f"[green]Rebuilt skill index: {len(entries)} skill(s).[/green] {root_note}")
    interval = settings.skills_index_watch_interval_seconds
    when = f"within {interval}s" if interval > 0 else "on its next request"
    console.print(f"[dim]A running `pincer run` applies this {when}; the dashboard on its next request.[/dim]")
