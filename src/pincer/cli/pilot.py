"""`pincer pilot` — pilot onboarding and review tooling (Sprint 10)."""

from __future__ import annotations

import typer

from pincer.cli._shared import console

pilot_app = typer.Typer(name="pilot", help="Pilot onboarding and review tooling (Sprint 10)")


@pilot_app.command(name="preflight")
def pilot_preflight(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """Check every onboarding prerequisite against the live configuration.

    Run this BEFORE the customer is on the call — discovering a missing Twilio
    number mid-session is what turns a 2h onboarding into a 4h one."""
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.onboarding import (
        TARGET_MINUTES,
        StepStatus,
        blocking,
        manual_minutes,
        preflight,
        total_minutes,
    )

    results = preflight(get_settings_relaxed())
    if output_json:
        console.print(_json.dumps([r.to_dict() for r in results], indent=2))
        raise typer.Exit(1 if blocking(results) else 0)

    icons = {
        StepStatus.READY: "[green]✅[/green]",
        StepStatus.MISSING: "[red]❌[/red]",
        StepStatus.MANUAL: "[cyan]☐[/cyan]",
        StepStatus.SKIPPED: "[dim]–[/dim]",
    }
    table = Table(show_header=True, title="Onboarding preflight")
    table.add_column("")
    table.add_column("Step")
    table.add_column("Min", justify="right")
    table.add_column("Detail")
    for result in results:
        table.add_row(icons[result.status], result.step.title, str(result.step.minutes), result.message)
    console.print(table)

    missing = blocking(results)
    console.print(
        f"\nEstimated: [bold]{total_minutes()} min[/bold] "
        f"({manual_minutes()} manual) against a {TARGET_MINUTES} min target."
    )
    if missing:
        console.print(f"\n[red]{len(missing)} blocking item(s) — fix before the onboarding session.[/red]\n")
    else:
        console.print("\n[green]No blocking items. Manual steps (☐) still need a human.[/green]\n")
    raise typer.Exit(1 if missing else 0)


@pilot_app.command(name="checklist")
def pilot_checklist(
    customer: str = typer.Argument(..., help="Customer name, used as the document title"),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Emit a per-customer onboarding checklist with a time-tracking table."""
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.onboarding import preflight, render_checklist

    markdown = render_checklist(customer, preflight(get_settings_relaxed()))
    if out:
        _Path(out).write_text(markdown, encoding="utf-8")
        console.print(f"[green]Checklist written to {out}[/green]")
    else:
        console.print(markdown)


@pilot_app.command(name="spot-check")
def pilot_spot_check(
    count: int = typer.Option(10, "--count", "-n", help="Calls to sample"),
    days: int = typer.Option(7, "--days", "-d", help="Window in days"),
    seed: int = typer.Option(0, "--seed", help="Sampling seed — same seed, same calls"),
    language: str = typer.Option("", "--language", "-l", help="Only calls in this language"),
    failures_only: bool = typer.Option(False, "--failures-only", help="Only calls that did not complete"),
    week: str = typer.Option("", "--week", help="Label for the sheet (e.g. 'week 2')"),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Weekly transcript spot-check sheet (T10.2).

    Ten calls, PII-masked, with the review questions attached. The sample is
    deterministic from --seed so two reviewers argue about the same calls."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.observability.pilot_review import render_spot_check, sample_calls

    calls = _asyncio.run(
        sample_calls(
            get_settings_relaxed(),
            count=count,
            days=days,
            seed=seed,
            language=language or None,
            only_failures=failures_only,
        )
    )
    if not calls:
        console.print(f"[yellow]No calls in the last {days} day(s) matching the filter.[/yellow]")
        raise typer.Exit(1)

    sheet = render_spot_check(calls, week=week)
    if out:
        _Path(out).write_text(sheet, encoding="utf-8")
        console.print(f"[green]Spot-check sheet ({len(calls)} calls) written to {out}[/green]")
    else:
        console.print(sheet)


@pilot_app.command(name="export-fixture")
def pilot_export_fixture(
    call_sid: str = typer.Argument(..., help="Call SID to turn into a harness persona"),
    name: str = typer.Option("", "--name", help="Fixture name (default: derived from the SID)"),
    notes: str = typer.Option("", "--notes", help="Why this call is worth replaying"),
    out: str = typer.Option("", "--out", "-o", help="Directory or file to write the fixture to"),
) -> None:
    """Export a real call as a PII-masked harness persona fixture (T10.3).

    Review the flagged names before committing — mask_pii handles numbers and
    emails, but personal names are not a pattern."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from pincer.config import get_settings_relaxed
    from pincer.observability.pilot_review import export_persona_fixture, fixture_to_json

    try:
        fixture = _asyncio.run(export_persona_fixture(get_settings_relaxed(), call_sid, name=name, notes=notes))
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    payload = fixture_to_json(fixture)
    if out:
        target = _Path(out)
        if target.is_dir():
            target = target / f"{fixture['name']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        console.print(f"[green]Fixture written to {target}[/green]")
    else:
        console.print(payload)

    flagged = fixture["review_required"]["possible_names"]
    if flagged:
        console.print(
            f"\n[yellow]⚠️  Possible personal names detected: {', '.join(flagged)}[/yellow]\n"
            "[yellow]Replace them with placeholders and empty `review_required.possible_names` "
            "before committing — the loader refuses an unreviewed fixture.[/yellow]\n"
        )
    else:
        console.print("\n[green]No personal names detected. Still read it once before committing.[/green]\n")


@pilot_app.command(name="automation-candidates")
def pilot_automation_candidates() -> None:
    """Manual onboarding steps ranked by minutes saved (T10.3 input).

    This is the list "automate the top 3 manual steps" refers to. It is derived
    from the step timings, so it changes when reality does."""
    from rich.table import Table

    from pincer.onboarding import (
        TARGET_MINUTES,
        automatable_minutes,
        automation_candidates,
        manual_minutes,
        total_minutes,
    )

    candidates = automation_candidates()
    table = Table(show_header=True, title="Onboarding automation candidates")
    table.add_column("Min saved", justify="right")
    table.add_column("Step")
    table.add_column("What automating it takes")
    for step in candidates:
        table.add_row(str(step.minutes), step.title, step.automation_note)
    console.print(table)

    remaining = total_minutes() - automatable_minutes()
    console.print(
        f"\nNow: [bold]{total_minutes()} min[/bold] ({manual_minutes()} manual). "
        f"Automating all {len(candidates)} candidates would reach ~{remaining} min "
        f"against the {TARGET_MINUTES} min target.\n"
    )
