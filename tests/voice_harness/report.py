"""Markdown reliability report for harness runs (Sprint 1, T1.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import ScenarioResult


def render_report(results: list[ScenarioResult]) -> str:
    """Render a per-run markdown report: success rate, latency, failure coverage."""
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    rate = (passed / total * 100) if total else 0.0

    latencies = [r.mean_turn_latency_s for r in results if r.mean_turn_latency_s is not None]
    mean_latency = sum(latencies) / len(latencies) if latencies else None

    lines = [
        "# Voice Reliability Report",
        "",
        f"**Scenarios:** {total}  |  **Passed:** {passed}  |  **Success rate:** {rate:.0f}% (target ≥ 90%)",
        f"**Mean turn latency (in-harness):** {f'{mean_latency * 1000:.0f}ms' if mean_latency is not None else 'n/a'}",
        "",
        "| Scenario | OK | Task done | Terminal phase | Turns | Final status | Unverified claims | Notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {'✅' if r.ok else '❌'} | {'yes' if r.task_done else 'no'} "
            f"| {r.terminal_phase} | {r.turns} | {'yes' if r.final_status_sent else 'NO'} "
            f"| {len(r.unverified_claims)} | {', '.join(r.notes) or '—'} |"
        )

    lines += [
        "",
        "## Failure-path coverage",
        "",
        "- Silent callee → phase-timeout watchdog (spoken exit)",
        "- Hostile callee → polite early exit",
        "- Wrong number → apology + hangup",
        "- Voicemail greeting → brief message, no conversation",
        "- Mid-call hangup by callee → cleanup + final status",
        "- Repeated agent-brain errors → apology + graceful FAILED",
        "- Garbled speech → ask-to-repeat, not guessing",
        "",
    ]
    return "\n".join(lines)
