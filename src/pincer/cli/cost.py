"""`pincer cost` — show API costs and spending breakdown."""

from __future__ import annotations

import typer

from pincer.cli._shared import console


async def cost(
    days: int = typer.Option(0, "--days", help="Show spending for last N days"),
    by_model: bool = typer.Option(False, "--by-model", help="Breakdown by LLM model"),
    by_tool: bool = typer.Option(False, "--by-tool", help="Breakdown by tool"),
    export: str = typer.Option("", "--export", help="Export cost data to JSON file"),
) -> None:
    """Show API costs and spending breakdown."""
    await _show_cost(days=days, by_model=by_model, by_tool=by_tool, export=export)


def _hit_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.1%}"


async def _show_cost(days: int = 0, by_model: bool = False, by_tool: bool = False, export: str = "") -> None:
    from datetime import UTC, datetime, timedelta

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.llm.cost_tracker import CostTracker
    from pincer.services.costs import cache_hit_rate

    settings = get_settings_relaxed()
    tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await tracker.initialize()

    today = await tracker.get_today_spend()
    summary = await tracker.get_summary()

    console.print("[bold]Pincer Cost Report[/bold]\n")
    console.print(f"  Today:   ${today:.4f} / ${settings.daily_budget_usd:.2f}")
    console.print(f"  Total:   ${summary.total_usd:.4f} ({summary.total_calls} calls)")
    console.print(f"  Tokens:  {summary.total_input_tokens:,} in / {summary.total_output_tokens:,} out")
    written = summary.total_cache_write_5m_tokens + summary.total_cache_write_1h_tokens
    console.print(
        f"  Cache:   {summary.total_cache_read_tokens:,} read / {written:,} written "
        f"({_hit_rate(summary.cache_hit_rate)} hit rate)"
    )

    if days > 0:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        history = await tracker.get_daily_history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if history:
            console.print(f"\n[bold]Last {days} days:[/bold]")
            table = Table()
            table.add_column("Date")
            table.add_column("Cost", justify="right")
            table.add_column("Requests", justify="right")
            for entry in history:
                table.add_row(entry["date"], f"${entry['total']:.4f}", str(entry["requests"]))
            console.print(table)

    if by_model:
        end = datetime.now(UTC)
        start = end - timedelta(days=max(days, 7))
        models = await tracker.get_costs_by_model(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if models:
            console.print("\n[bold]By Model:[/bold]")
            table = Table()
            table.add_column("Model", no_wrap=True)
            table.add_column("Cost", justify="right")
            table.add_column("Requests", justify="right")
            table.add_column("Tokens", justify="right")
            table.add_column("Cache read", justify="right")
            table.add_column("Cache write", justify="right")
            table.add_column("Hit rate", justify="right")
            for m in models:
                written = m["cache_write_5m_tokens"] + m["cache_write_1h_tokens"]
                table.add_row(
                    m["model"],
                    f"${m['total']:.4f}",
                    str(m["requests"]),
                    f"{m['tokens']:,}",
                    f"{m['cache_read_tokens']:,}",
                    f"{written:,}",
                    _hit_rate(cache_hit_rate(m["input_tokens"], written, m["cache_read_tokens"])),
                )
            console.print(table)

    if export:
        import json as _json
        from pathlib import Path as _P

        end = datetime.now(UTC)
        start = end - timedelta(days=max(days, 30))
        history = await tracker.get_daily_history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        _P(export).write_text(
            _json.dumps(
                {
                    "history": history,
                    "summary": {
                        "total_usd": summary.total_usd,
                        "total_calls": summary.total_calls,
                    },
                },
                indent=2,
            )
        )
        console.print(f"\n[green]Exported to {export}[/green]")

    await tracker.close()
