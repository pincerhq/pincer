"""`pincer audit` — view and export audit logs."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

audit_app = AsyncTyper(name="audit", help="View and export audit logs")


@audit_app.callback(invoke_without_command=True)
async def audit_default(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", help="Number of entries"),
    action: str = typer.Option("", "--action", help="Filter by action type"),
    user: str = typer.Option("", "--user", help="Filter by user ID"),
    since: str = typer.Option("", "--since", help="Filter from date (ISO)"),
    export: str = typer.Option("", "--export", help="Export to JSON file"),
) -> None:
    """View audit log entries."""
    if ctx.invoked_subcommand is not None:
        return
    await _show_audit(limit=limit, action=action, user=user, since=since, export=export)


async def _show_audit(
    limit: int = 50,
    action: str = "",
    user: str = "",
    since: str = "",
    export: str = "",
) -> None:
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.security.audit import AuditAction, AuditLogger

    settings = get_settings_relaxed()
    logger = AuditLogger(db_path=settings.db_path)
    await logger.initialize()

    if export:
        count = await logger.export_json(
            export,
            user_id=user or None,
            since=since or None,
        )
        console.print(f"[green]Exported {count} entries to {export}[/green]")
        await logger.shutdown()
        return

    action_filter = None
    if action:
        try:
            action_filter = AuditAction(action)
        except ValueError:
            console.print(f"[red]Invalid action: {action}[/red]")
            console.print(f"Valid actions: {', '.join(a.value for a in AuditAction)}")
            await logger.shutdown()
            return

    results = await logger.query(
        user_id=user or None,
        action=action_filter,
        since=since or None,
        limit=limit,
    )

    if not results:
        console.print("[dim]No audit entries found.[/dim]")
        await logger.shutdown()
        return

    table = Table(title=f"Audit Log (last {len(results)})")
    table.add_column("Time", width=20)
    table.add_column("User", width=12)
    table.add_column("Action", width=16)
    table.add_column("Tool", width=15)
    table.add_column("Summary")
    table.add_column("Cost", justify="right", width=10)

    for row in results:
        ts = (row.get("timestamp") or "")[:19]
        cost_str = f"${row.get('cost_usd', 0):.4f}" if row.get("cost_usd") else ""
        summary = (row.get("input_summary") or "")[:60]
        table.add_row(
            ts,
            str(row.get("user_id", ""))[:12],
            str(row.get("action", "")),
            str(row.get("tool", "") or ""),
            summary,
            cost_str,
        )

    console.print(table)

    stats = await logger.get_stats()
    console.print(
        f"\n  Total: {stats['total_entries']} entries  "
        f"Cost: ${stats['total_cost_usd']:.4f}  "
        f"Failed: {stats['failed_actions']}"
    )
    await logger.shutdown()
