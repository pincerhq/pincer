"""`pincer telephony` — latency baselines and per-call diagnosis from the terminal.

The dashboard is the primary surface; this exists for the two things a terminal
is better at: establishing alert thresholds from observed baselines before any
threshold has been set, and pulling one call's turn breakdown into a paste-able
form during an incident.
"""

from __future__ import annotations

import asyncio
import json as json_lib
from typing import Any

import typer
from rich.table import Table

from pincer.cli._shared import console

telephony_app = typer.Typer(name="telephony", help="Telephony telemetry: latency baselines and call diagnosis")

#: Headroom over the observed p95 when suggesting an alert threshold. An alert
#: set exactly at today's p95 fires on half of tomorrow's normal traffic.
_THRESHOLD_HEADROOM = 1.5


def _settings() -> Any:
    from pincer.config import get_settings_relaxed

    return get_settings_relaxed()


def _ms(value: float | None) -> str:
    if value is None:
        return "[dim]—[/dim]"
    return f"{value / 1000:.2f}s" if value >= 1000 else f"{value:.0f}ms"


@telephony_app.command(name="baseline")
def baseline(
    hours: float = typer.Option(168.0, "--hours", "-h", help="Window to measure over"),
    engine: str = typer.Option("", "--engine", help="Restrict to one engine"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Observed per-stage percentiles, and the alert thresholds they suggest.

    Set `PINCER_ALERT_*` from *these* numbers rather than from the shipped
    defaults: a threshold that does not reflect your traffic either never fires
    or never stops.
    """
    from pincer.voice.telemetry import queries
    from pincer.voice.telemetry.schema import METRICS

    settings = _settings()
    filters = queries.CallFilters.for_hours(hours, engine=engine)
    aggregate = asyncio.run(
        queries.overview(
            settings.db_path,
            filters,
            min_samples=int(getattr(settings, "telephony_min_samples", 20) or 20),
        )
    )

    if output_json:
        console.print_json(json_lib.dumps(aggregate.to_dict()))
        return

    coverage = aggregate.coverage
    console.print(
        f"[bold]Telephony baseline[/bold] — last {hours:g}h, "
        f"{coverage.get('calls', 0)} call(s), {coverage.get('turns', 0)} turn(s)"
    )
    if coverage.get("telemetry_tables") is False:
        console.print("[yellow]Telemetry tables are missing — run `pincer db upgrade`.[/yellow]")
        return
    if not coverage.get("turns"):
        console.print("[yellow]No turns in this window; nothing to baseline.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("Stage", "p50", "p95", "p99", "max", "samples", "source"):
        table.add_column(column, justify="right" if column not in ("Stage", "source") else "left")

    for key, summary in aggregate.stages.items():
        if not summary["count"]:
            continue
        definition = METRICS.get(key.replace("_ms", "") + "_ms") or METRICS.get(key)
        table.add_row(
            key,
            _ms(summary["p50"]),
            _ms(summary["p95"]),
            _ms(summary["p99"]),
            _ms(summary["max"]),
            f"{summary['count']}" + ("" if summary["sufficient_samples"] else " [yellow](low)[/yellow]"),
            str(definition.source) if definition else "",
        )
    console.print(table)

    response = aggregate.stages.get("response_latency_ms", {})
    queue = aggregate.stages.get("audio_queue_ms", {})
    console.print("\n[bold]Suggested thresholds[/bold] (observed p95 × 1.5, rounded):")
    for env_var, summary in (
        ("PINCER_ALERT_RESPONSE_LATENCY_P95_MS", response),
        ("PINCER_ALERT_AUDIO_QUEUE_P95_MS", queue),
    ):
        p95 = summary.get("p95")
        if p95 is None:
            console.print(f"  {env_var}=[dim]no samples[/dim]")
        elif not summary.get("sufficient_samples"):
            suggested = round(p95 * _THRESHOLD_HEADROOM, -1)
            console.print(
                f"  {env_var}=[yellow]{suggested:g}  (only {summary['count']} samples — collect more first)[/yellow]"
            )
        else:
            console.print(f"  {env_var}={round(p95 * _THRESHOLD_HEADROOM, -1):g}")

    rates = aggregate.rates
    console.print("\n[bold]Observed rates[/bold] (denominators on the dashboard):")
    for key, label in (
        ("connection_rate", "connection"),
        ("technical_failure_rate", "technical failure"),
        ("unexpected_disconnect_rate", "unexpected disconnect"),
    ):
        entry = rates.get(key, {})
        value = entry.get("value")
        shown = "[dim]n/a[/dim]" if value is None else f"{value:.1%}"
        console.print(f"  {label}: {shown}  ({entry.get('numerator', 0)}/{entry.get('denominator', 0)})")

    if aggregate.unavailable:
        console.print("\n[bold]Not measurable on the engines seen[/bold]:")
        for metric in aggregate.unavailable:
            console.print(f"  [dim]{metric['key']}[/dim]: {metric['unavailable_reason']}")


@telephony_app.command(name="call")
def call_detail(
    call_ref: str = typer.Argument(..., help="Provider CallSid or internal call id"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """One call's turn breakdown, with the measured critical path per turn."""
    from pincer.voice.telemetry import queries

    settings = _settings()

    async def _load() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        row = await queries.get_call(settings.db_path, call_ref)
        if row is None:
            return None, []
        return row, await queries.get_turns(settings.db_path, row["call_id"])

    call, turns = asyncio.run(_load())
    if call is None:
        console.print(f"[red]No telemetry for {call_ref}[/red]")
        raise typer.Exit(1)

    if output_json:
        console.print_json(json_lib.dumps({"call": call, "turns": turns}, default=str))
        return

    console.print(
        f"[bold]{call['provider_call_id'] or call['call_id']}[/bold] "
        f"{call['direction']} · {call['engine']} · {call['status']}"
        + (f" · {call['failure_code']}" if call["failure_code"] not in ("", "none") else "")
    )
    console.print(
        f"  registered {call['registered_at']} · answered {call['answered_at'] or '[dim]never[/dim]'} "
        f"· setup {_ms(call['setup_ms'])} · duration {_ms(call['duration_ms'])}"
    )
    if not call.get("sampled", True):
        console.print("  [yellow]Not sampled: lifecycle only, no turn detail.[/yellow]")

    if not turns:
        console.print("  [dim]No turn telemetry.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("#", "response", "source", "llm ttft", "tts 1st", "tools", "bottleneck", "flags"):
        table.add_column(column)
    for turn in turns:
        flags = []
        if turn["cancelled"]:
            flags.append("barge-in" if turn["interrupted"] else "cancelled")
        if turn["error"]:
            flags.append(turn["error"])
        if not turn["complete"]:
            flags.append("incomplete")
        table.add_row(
            str(turn["turn_no"]),
            _ms(turn["response_latency_ms"]),
            turn["response_latency_source"],
            _ms(turn["llm_ttft_ms"]),
            _ms(turn["tts_first_audio_ms"]),
            str(turn["tool_calls"]),
            f"{turn['bottleneck_stage']} ({_ms(turn['bottleneck_ms'])})" if turn["bottleneck_stage"] else "",
            ", ".join(flags),
        )
    console.print(table)
    console.print(
        "[dim]`response` is caller speech end → first response audio SENT to the provider. "
        "On conversation_relay the clock starts at transcript arrival instead, so the real "
        "figure is larger by the endpointing wait.[/dim]"
    )


@telephony_app.command(name="health")
def health() -> None:
    """Exporter health: dropped records and export failures."""
    from pincer.voice.telemetry import runtime

    state = runtime.health()
    export = state["export"]
    console.print(f"enabled: {state['enabled']} · sample rate: {state['sample_rate']:.2f}")
    console.print(
        f"queued {export['queued']} · exported {export['exported']} · "
        f"dropped {export['dropped_queue_full']} · failures {export['export_failures']}"
    )
    if export["last_error"]:
        console.print(f"[red]last error: {export['last_error']}[/red]")
    if not state["healthy"] and state["enabled"]:
        console.print("[yellow]Telemetry is degraded — latency charts are incomplete.[/yellow]")
    console.print("[dim]This reports THIS process. A dashboard served by another process has its own counters.[/dim]")


@telephony_app.command(name="alerts")
def alerts(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """Evaluate the telephony alert rules right now."""
    from pincer.voice.telemetry import alerts as telephony_alerts

    settings = _settings()
    rows = asyncio.run(telephony_alerts.evaluate(settings.db_path, settings))

    if output_json:
        console.print_json(json_lib.dumps([row.to_dict() for row in rows]))
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("rule", "state", "value", "threshold", "samples", "why"):
        table.add_column(column)
    for row in rows:
        if row.firing:
            state = f"[red]{row.severity.upper()}[/red]"
        elif row.insufficient_data:
            state = "[dim]no data[/dim]"
        else:
            state = "[green]ok[/green]"
        table.add_row(
            row.rule,
            state,
            "—" if row.value is None else f"{row.value:g}",
            f"{row.threshold:g}",
            f"{row.samples}" + (f"/{row.min_samples}" if row.min_samples else ""),
            row.reason or "",
        )
    console.print(table)
