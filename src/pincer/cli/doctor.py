"""`pincer doctor` — run security checks with a traffic-light report."""

from __future__ import annotations

import typer

from pincer.cli._shared import console


def doctor(
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run 25+ security checks with traffic-light report."""
    import json as _json
    from pathlib import Path as _P

    from pincer.security.doctor import CheckStatus, SecurityDoctor

    doc = SecurityDoctor(
        data_dir=_P("data"),
        config_dir=_P("."),
    )
    report = doc.run_all()

    if output_json:
        console.print(_json.dumps(report.to_dict(), indent=2))
        return

    from rich.table import Table

    status_icons = {
        CheckStatus.PASS: "[green]✅[/green]",
        CheckStatus.WARNING: "[yellow]⚠️[/yellow]",
        CheckStatus.CRITICAL: "[red]❌[/red]",
        CheckStatus.SKIPPED: "[dim]➖[/dim]",
    }

    console.print(
        f"\n[bold]Pincer Security Doctor[/bold]  "
        f"Score: [{'green' if report.score >= 80 else 'yellow' if report.score >= 60 else 'red'}]"
        f"{report.score}/100[/]\n"
    )

    current_category = ""
    table = Table(show_header=True)
    table.add_column("", width=4)
    table.add_column("Check", style="bold")
    table.add_column("Message")
    table.add_column("Fix", style="dim")

    for check in report.checks:
        if check.category != current_category:
            current_category = check.category
            table.add_row("", f"[bold underline]{current_category.upper()}[/bold underline]", "", "")
        table.add_row(
            status_icons.get(check.status, ""),
            check.name,
            check.message,
            check.fix_hint,
        )

    console.print(table)
    console.print(
        f"\n  [green]{report.passed} passed[/green]  "
        f"[yellow]{report.warnings} warnings[/yellow]  "
        f"[red]{report.critical} critical[/red]\n"
    )
