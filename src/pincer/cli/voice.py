"""`pincer voice` — ElevenLabs voices, ops/golden signals, and the do-not-call list."""

from __future__ import annotations

import typer

from pincer.cli._shared import console

voice_app = typer.Typer(name="voice", help="Manage ElevenLabs voices for voice calling")


ops_app = typer.Typer(name="ops", help="Voice operations: golden signals, SLOs, canary, digest (Sprint 9)")
voice_app.add_typer(ops_app, name="ops")


def _signal_row(signal: dict) -> tuple[str, str, str, str]:
    """(name, value, target, sample) formatted for the golden-signal table."""
    value, unit, target = signal.get("value"), signal.get("unit", ""), signal.get("target")
    if not signal.get("sufficient_data"):
        shown = f"[dim]n/a ({signal.get('sample_size', 0)}/{signal.get('min_sample', 1)} samples)[/dim]"
    elif unit == "ratio":
        shown = f"{value:.1%}"
    elif unit == "count":
        shown = str(int(value or 0))
    elif unit == "ratio_to_baseline":
        shown = f"{value:.2f}×"
    else:
        shown = f"{value:.2f}{unit}"
    goal = ""
    if target is not None:
        if unit == "ratio":
            goal = f"{target:.0%}"
        elif unit == "ratio_to_baseline":
            goal = f"{target:g}× baseline"
        elif unit == "count":
            goal = f"{target:g}"
        else:
            goal = f"{target:g}{unit}"
    return signal.get("name", ""), shown, goal, f"{signal.get('sample_size', 0)} / {signal.get('window', '')}"


@ops_app.command(name="status")
def voice_ops_status(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """The five golden signals and any alert that would fire right now.

    First command on the on-call quick card — one screen that answers
    "is voice healthy?" without a browser or a metrics backend."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability import golden_signals as _gs
    from pincer.observability.alerts import disk_alert, evaluate

    settings = get_settings_relaxed()

    async def _collect():
        signals = await _gs.collect(settings)
        alerts = evaluate(signals, settings)
        host = disk_alert(settings)
        if host is not None:
            alerts.insert(0, host)
        return signals, alerts

    signals, alerts = _asyncio.run(_collect())

    if output_json:
        console.print(
            _json.dumps(
                {
                    **signals.to_dict(),
                    "alerts": [
                        {"rule": a.rule, "severity": str(a.severity), "title": a.title, "detail": a.detail}
                        for a in alerts
                    ],
                },
                indent=2,
            )
        )
        return

    table = Table(show_header=True, title="Voice golden signals")
    table.add_column("Signal")
    table.add_column("Value", justify="right")
    table.add_column("Threshold", justify="right")
    table.add_column("Sample / window")
    for signal in signals.to_dict()["signals"].values():
        table.add_row(*_signal_row(signal))
    console.print(table)

    if not alerts:
        console.print("\n[green]No alerts firing.[/green]\n")
        return
    console.print("")
    for alert in alerts:
        color = "red" if str(alert.severity) == "page" else "yellow"
        console.print(f"[{color}]{alert.render()}[/{color}]\n")


@ops_app.command(name="slo")
def voice_ops_slo(output_json: bool = typer.Option(False, "--json", help="Output as JSON")) -> None:
    """Month-to-date SLO status and error-budget burn (T9.5)."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.slo import collect

    report = _asyncio.run(collect(get_settings_relaxed()))
    if output_json:
        console.print(_json.dumps(report, indent=2))
        return

    table = Table(show_header=True, title=f"SLOs — {report['slos'][0]['window'] if report['slos'] else ''}")
    table.add_column("SLO")
    table.add_column("Actual", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Budget burned", justify="right")
    table.add_column("n", justify="right")
    for slo in report["slos"]:
        actual, unit = slo["actual"], slo["unit"]
        if actual is None:
            shown = "[dim]no data[/dim]"
        elif unit == "ratio":
            shown = f"{actual:.2%}"
        else:
            shown = f"{actual:.2f}{unit}"
        target = f"{slo['target']:.1%}" if unit == "ratio" else f"{slo['target']:g}{unit}"
        burn = slo["burn_pct"]
        burn_text = "[dim]—[/dim]" if burn is None else f"{'[red]' if burn > 100 else ''}{burn:.0f}%"
        label = slo["name"] + (" [dim](inferred)[/dim]" if slo["confidence"] == "inferred" else "")
        table.add_row(label, shown, target, burn_text, str(slo["sample_size"]))
    console.print(table)

    if report["feature_freeze"]:
        console.print(f"\n[red]🧊 Feature freeze in effect: {report['freeze_reason']}[/red]\n")
    else:
        console.print(
            f"\n[green]No feature freeze[/green] "
            f"[dim](threshold {report['freeze_threshold_pct']:.0f}% burn, "
            f"min {report['freeze_min_sample']} samples)[/dim]\n"
        )


@ops_app.command(name="canary")
def voice_ops_canary(
    history: bool = typer.Option(False, "--history", help="Show recent runs instead of placing a call"),
) -> None:
    """Run the synthetic canary call now, or show recent runs.

    Without --history this places a REAL phone call to
    PINCER_VOICE_CANARY_NUMBER through the normal abuse gate."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.canary import recent_runs, run_canary

    settings = get_settings_relaxed()

    if history:
        runs = _asyncio.run(recent_runs(settings, limit=20))
        if not runs:
            console.print("[yellow]No canary runs recorded yet.[/yellow]")
            return
        table = Table(show_header=True, title="Canary runs")
        table.add_column("When")
        table.add_column("Result")
        table.add_column("Turns", justify="right")
        table.add_column("Duration", justify="right")
        table.add_column("Reason")
        for run in runs:
            if run["skipped"]:
                result = "[yellow]skipped[/yellow]"
            elif run["ok"]:
                result = "[green]ok[/green]"
            else:
                result = "[red]FAILED[/red]"
            table.add_row(
                str(run["ran_at"])[:19],
                result,
                str(run["turns"]),
                f"{run['duration_s']:.0f}s",
                str(run["reason"] or "")[:60],
            )
        console.print(table)
        return

    if not settings.voice_canary_enabled:
        console.print("[yellow]Canary is disabled. Set PINCER_VOICE_CANARY_ENABLED=true.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[cyan]Placing canary call to {settings.voice_canary_number}...[/cyan]")
    result = _asyncio.run(run_canary(settings))
    console.print(result.render())
    if not result.ok:
        raise typer.Exit(1)


@ops_app.command(name="ga-gate")
def voice_ops_ga_gate(
    days: int = typer.Option(14, "--days", "-d", help="Pilot window in days"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output the sign-off document"),
    out: str = typer.Option("", "--out", "-o", help="Write the markdown report to this file"),
) -> None:
    """Evaluate the GA exit criteria against real pilot data (Sprint 10, T10.4).

    Exit code 0 only when EVERY criterion passes — 'insufficient data' is not a
    pass, so this is safe to wire into a release gate."""
    import asyncio as _asyncio
    import json as _json

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.ga_gate import Verdict, evaluate, render_markdown

    report = _asyncio.run(evaluate(get_settings_relaxed(), days=days))

    if out:
        from pathlib import Path as _Path

        _Path(out).write_text(render_markdown(report), encoding="utf-8")
        console.print(f"[green]Sign-off document written to {out}[/green]")
    if output_json:
        console.print(_json.dumps(report.to_dict(), indent=2, default=str))
    elif markdown:
        console.print(render_markdown(report))
    else:
        icons = {
            Verdict.PASS: "[green]✅ pass[/green]",
            Verdict.FAIL: "[red]❌ fail[/red]",
            Verdict.INSUFFICIENT: "[yellow]⏳ no data[/yellow]",
            Verdict.MANUAL: "[cyan]🧑 manual[/cyan]",
        }
        table = Table(show_header=True, title=f"GA gate — last {days} days")
        table.add_column("Result")
        table.add_column("Criterion")
        table.add_column("Evidence")
        for criterion in report.criteria:
            table.add_row(icons[criterion.verdict], criterion.title, criterion.summary)
        console.print(table)

        if report.ready:
            console.print("\n[green]✅ READY FOR GA — every criterion met.[/green]\n")
        else:
            console.print(
                f"\n[red]🚫 NOT READY[/red] — {len(report.failed)} failed, {len(report.blocked)} undecided.\n"
            )
            for criterion in report.failed + report.blocked:
                if criterion.needed:
                    console.print(f"  [dim]{criterion.key}:[/dim] {criterion.needed}")
            console.print("")

    raise typer.Exit(0 if report.ready else 1)


@ops_app.command(name="digest")
def voice_ops_digest() -> None:
    """Render the weekly failure digest without sending it."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.observability.digest import build_digest

    console.print(_asyncio.run(build_digest(get_settings_relaxed())))


@ops_app.command(name="failures")
def voice_ops_failures(
    hours: float = typer.Option(168.0, "--hours", "-h", help="Window in hours (default: 7 days)"),
) -> None:
    """Failure codes over a window, ranked (T9.3)."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.observability.failure_codes import describe
    from pincer.observability.golden_signals import call_success_rate

    signal = _asyncio.run(call_success_rate(get_settings_relaxed(), window_hours=hours))
    by_code = signal.detail.get("by_failure_code") or {}
    if not by_code:
        console.print(f"[yellow]No terminated calls in the last {hours:g}h.[/yellow]")
        return

    table = Table(show_header=True, title=f"Failure codes — last {hours:g}h ({signal.sample_size} calls)")
    table.add_column("Code")
    table.add_column("Count", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Meaning")
    for code, count in sorted(by_code.items(), key=lambda kv: kv[1], reverse=True):
        share = count / signal.sample_size if signal.sample_size else 0.0
        table.add_row(code, str(count), f"{share:.0%}", describe(code))
    console.print(table)


dnc_app = typer.Typer(name="dnc", help="Do-not-call list — numbers Pincer will never dial (Sprint 8, T8.3)")
voice_app.add_typer(dnc_app, name="dnc")


@dnc_app.command(name="list")
def voice_dnc_list() -> None:
    """Show the shared do-not-call list (blocks every user and channel)."""
    import asyncio as _asyncio

    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.voice.safety_gates import list_do_not_call

    entries = _asyncio.run(list_do_not_call(get_settings_relaxed()))
    if not entries:
        console.print("[green]Do-not-call list is empty.[/green]")
        return

    table = Table(show_header=True, title=f"Do-not-call list ({len(entries)} number(s))")
    table.add_column("Number")
    table.add_column("Source")
    table.add_column("Reason")
    table.add_column("Added")
    for entry in entries:
        table.add_row(
            entry.get("phone_number", ""),
            entry.get("source", "") or "-",
            entry.get("reason", "") or "-",
            (entry.get("added_at", "") or "")[:19],
        )
    console.print(table)


@dnc_app.command(name="add")
def voice_dnc_add(
    number: str = typer.Argument(..., help="Phone number in E.164 format, e.g. +4915112345678"),
    reason: str = typer.Option("", "--reason", "-r", help="Why this number is blocked"),
) -> None:
    """Block a number. Applies immediately to every channel and every user."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.voice.outbound import validate_e164
    from pincer.voice.safety_gates import add_do_not_call

    validated = validate_e164(number)
    if not validated:
        console.print(f"[red]Invalid phone number: {number}. Use E.164 format (e.g. +4915112345678).[/red]")
        raise typer.Exit(1)

    added = _asyncio.run(add_do_not_call(get_settings_relaxed(), validated, reason=reason, source="cli"))
    if added:
        console.print(f"[green]{validated} added to the do-not-call list.[/green]")
    else:
        console.print(f"[yellow]{validated} was already on the do-not-call list (reason updated).[/yellow]")


@dnc_app.command(name="remove")
def voice_dnc_remove(
    number: str = typer.Argument(..., help="Phone number to unblock"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Unblock a number — only ever do this with the callee's consent."""
    import asyncio as _asyncio

    from pincer.config import get_settings_relaxed
    from pincer.voice.safety_gates import remove_do_not_call

    if not yes and not typer.confirm(f"Remove {number} from the do-not-call list?"):
        raise typer.Abort

    removed = _asyncio.run(remove_do_not_call(get_settings_relaxed(), number))
    if removed:
        console.print(f"[green]{number} removed from the do-not-call list.[/green]")
    else:
        console.print(f"[yellow]{number} was not on the do-not-call list.[/yellow]")
        raise typer.Exit(1)


@voice_app.command(name="latency-report")
def voice_latency_report(
    calls: int = typer.Option(20, "--calls", "-n", help="Number of most recent calls to analyze"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """p50/p95 per latency stage from recent voice turns (Sprint 5, T5.1).

    Reads data/logs/voice_latency.jsonl, written one line per streamed turn."""
    import json as _json

    from pincer.config import get_settings_relaxed
    from pincer.voice.latency_report import build_latency_report, read_turn_records

    settings = get_settings_relaxed()
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    records = read_turn_records(path, last_calls=calls)
    if not records:
        console.print(f"[yellow]No turn records in {path} — run some calls first.[/yellow]")
        raise typer.Exit(1)

    report = build_latency_report(records)
    if output_json:
        console.print(_json.dumps(report, indent=2))
        return

    from rich.table import Table

    header = (
        f"\n[bold]Voice latency report[/bold] — {report['turns']} turn(s) "
        f"across {report['calls']} call(s), engines: {', '.join(report['engines']) or '-'}\n"
    )
    console.print(header)
    table = Table(show_header=True)
    table.add_column("Stage")
    table.add_column("p50 ms", justify="right")
    table.add_column("p95 ms", justify="right")
    table.add_column("n", justify="right")
    for stage, stats in report["stages"].items():
        table.add_row(stage, f"{stats['p50']:.0f}", f"{stats['p95']:.0f}", str(stats["n"]))
    console.print(table)
    total = report["stages"].get("total_ms")
    if total:
        target_ok = total["p50"] <= 1200 and total["p95"] <= 2000
        color = "green" if target_ok else "red"
        console.print(f"\n[{color}]Target p50 ≤ 1200ms / p95 ≤ 2000ms: {'MET' if target_ok else 'NOT MET'}[/{color}]\n")


@voice_app.command(name="latency-model")
def voice_latency_model(
    calls: int = typer.Option(20, "--calls", "-n", help="Number of most recent calls to analyze"),
    sort: str = typer.Option("p50", "--sort", help="Row order: 'p50' (fastest total first) or 'name'"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Latency per LLM model — p50/p95 of each stage grouped by the model that served the turn.

    Reads data/logs/voice_latency.jsonl (the ``turn_model`` stamp on each turn);
    turns without a model stamp are reported under 'unknown'."""
    import json as _json

    from pincer.config import get_settings_relaxed
    from pincer.voice.latency_report import MODEL_STAGES, build_model_report, read_turn_records

    if sort not in ("p50", "name"):
        console.print(f"[red]--sort must be 'p50' or 'name', got {sort!r}[/red]")
        raise typer.Exit(2)

    settings = get_settings_relaxed()
    path = settings.data_dir / "logs" / "voice_latency.jsonl"
    records = read_turn_records(path, last_calls=calls)
    if not records:
        console.print(f"[yellow]No turn records in {path} — run some calls first.[/yellow]")
        raise typer.Exit(1)

    rows = build_model_report(records, sort=sort)
    if output_json:
        console.print(_json.dumps(rows, indent=2))
        return

    from rich.table import Table

    total_turns = sum(row["turns"] for row in rows)
    total_calls = len({str(r.get("call_sid")) for r in records})
    console.print(
        f"\n[bold]Voice latency by model[/bold] — {total_turns} turn(s) across {total_calls} call(s), "
        f"{len(rows)} model(s)\n"
    )
    stage_labels = {
        "total_ms": "total",
        "llm_first_token_ms": "first token",
        "first_dispatch_ms": "first dispatch",
        "llm_done_ms": "llm done",
    }
    table = Table(show_header=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Turns", justify="right")
    table.add_column("Calls", justify="right")
    table.add_column("Err", justify="right")
    for stage in MODEL_STAGES:
        table.add_column(f"{stage_labels.get(stage, stage)}\np50 / p95", justify="right")
    for row in rows:
        cells = [row["model"], str(row["turns"]), str(row["calls"]), str(row["errors"])]
        for stage in MODEL_STAGES:
            stats = row["stages"].get(stage)
            cells.append(f"{stats['p50']:.0f} / {stats['p95']:.0f}" if stats else "-")
        table.add_row(*cells)
    console.print(table)
    console.print(
        "\n[dim]Times in ms, fastest total p50 first. "
        "'unknown' = turns recorded before the model stamp or that failed pre-LLM.[/dim]\n"
    )


@voice_app.command(name="list")
def voice_list() -> None:
    """List ElevenLabs voices (ID, name, category, languages) — find your cloned voice's ID here."""
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.voice.voices import VoiceLookupError, configured_voice_ids, list_voices

    settings = get_settings_relaxed()
    api_key = settings.elevenlabs_api_key.get_secret_value()
    if not api_key:
        console.print("[red]PINCER_ELEVENLABS_API_KEY not set.[/red]")
        raise typer.Exit(1)

    try:
        voices = list_voices(api_key)
    except VoiceLookupError as e:
        console.print(f"[red]Could not list voices: {e}[/red]")
        raise typer.Exit(1) from e

    configured = configured_voice_ids(settings)
    table = Table(title=f"ElevenLabs Voices ({len(voices)})")
    table.add_column("Voice ID", style="cyan")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Languages")
    table.add_column("")
    for voice in voices:
        table.add_row(
            voice.voice_id,
            voice.name,
            voice.category,
            ", ".join(voice.languages) or "-",
            "[green]configured[/green]" if voice.voice_id in configured else "",
        )
    console.print(table)
    console.print(
        "\n[dim]Set PINCER_ELEVENLABS_VOICE_ID (or _EN / _DE) to a Voice ID above, "
        "then judge it at telephony quality with `pincer voice test`.[/dim]"
    )


@voice_app.command(name="test")
def voice_test(
    voice_id: str = typer.Option("", "--voice-id", help="Voice ID to test (default: the configured voice)"),
    language: str = typer.Option("", "--language", help="Call language the voice is resolved for (en/de)"),
    text: str = typer.Option("", "--text", help="Custom sample text"),
) -> None:
    """Synthesize a sample to ~/.pincer/voice_test.wav (+ 8kHz mu-law variant at telephony quality)."""
    from pathlib import Path

    from pincer.config import get_settings_relaxed
    from pincer.voice.language import elevenlabs_model_for, resolve_call_language, voice_for
    from pincer.voice.voices import VoiceLookupError, synthesize_sample, ulaw_to_wav

    settings = get_settings_relaxed()
    api_key = settings.elevenlabs_api_key.get_secret_value()
    if not api_key:
        console.print("[red]PINCER_ELEVENLABS_API_KEY not set.[/red]")
        raise typer.Exit(1)

    lang = resolve_call_language(settings, language)
    resolved_voice = voice_id.strip() or voice_for(settings, lang)
    model = elevenlabs_model_for(settings, lang)
    samples = {
        "en": "Hello! I'm your personal assistant. This is how I sound on the phone.",
        "de": "Guten Tag! Ich bin Ihr persönlicher Assistent. So klinge ich am Telefon.",
        "uk": "Вітаю! Я ваш особистий асистент. Ось так я звучу по телефону.",
    }
    sample_text = text.strip() or samples.get(lang, samples["en"])

    out_dir = Path.home() / ".pincer"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "voice_test.wav"
    ulaw_path = out_dir / "voice_test_ulaw.wav"

    console.print(f"Synthesizing with voice [cyan]{resolved_voice}[/cyan], model {model}, language {lang}...")
    try:
        wav_path.write_bytes(synthesize_sample(api_key, resolved_voice, sample_text, model, "wav_16000"))
        ulaw_raw = synthesize_sample(api_key, resolved_voice, sample_text, model, "ulaw_8000")
        ulaw_path.write_bytes(ulaw_to_wav(ulaw_raw))
    except VoiceLookupError as e:
        console.print(f"[red]Synthesis failed: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]Wrote {wav_path}[/green] (16 kHz)")
    console.print(f"[green]Wrote {ulaw_path}[/green] (8 kHz mu-law — how callers will hear it)")
    console.print("[dim]Some voices sound very different at telephony quality — judge the mu-law file.[/dim]")
