"""`pincer db` — manage the Pincer database schema (Alembic wrappers)."""

from __future__ import annotations

from async_typer import AsyncTyper

from pincer.cli._shared import console

db_app = AsyncTyper(name="db", help="Manage the Pincer database schema")


@db_app.command(name="upgrade")
def db_upgrade() -> None:
    """Apply any pending schema migrations.

    Runs automatically whenever the app starts — this is for manual/ops use
    (e.g. applying migrations ahead of a deploy).
    """
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.upgrade(build_config(settings.db_path), "head")
    console.print(f"[green]Database at head: {settings.db_path}[/green]")


@db_app.command(name="current")
def db_current() -> None:
    """Show the currently applied migration revision."""
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.current(build_config(settings.db_path), verbose=True)


@db_app.command(name="history")
def db_history() -> None:
    """Show the full migration history."""
    from alembic import command as alembic_command

    from pincer.config import get_settings_relaxed
    from pincer.db import build_config

    settings = get_settings_relaxed()
    alembic_command.history(build_config(settings.db_path), verbose=True)
