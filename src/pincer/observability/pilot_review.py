"""
Pilot review tooling (Sprint 10, T10.2/T10.3).

Two jobs the weekly pilot cadence needs and nothing else provides:

**Spot-checks.** Ten calls a week, read by a human, looking for the things no
metric catches: did the German sound like a person, were dates and names heard
correctly, did the negotiation behave. `sample_calls` picks them
*deterministically from a seed* so two reviewers looking at "week 3, seed 3" see
the same ten calls and can disagree about the same evidence.

**Fixtures from reality.** The harness personas were written from imagination in
Sprint 1. After a pilot, the interesting failures are real callees doing things
nobody imagined — so `export_persona_fixture` turns an actual call into a
replayable, PII-masked persona. That is what stops a pilot bug from being fixed
once and regressing quietly later.

Everything that leaves this module is masked by `mask_pii` first. A fixture is a
file that gets committed to a repository, so a leak here is permanent.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.observability.failure_codes import describe
from pincer.voice.pii_guard import mask_phone_number, mask_pii

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ReviewCall:
    call_sid: str
    direction: str
    started_at: str
    duration_seconds: int
    language: str
    failure_code: str
    from_number: str
    to_number: str
    cost_usd: float | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "direction": self.direction,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "language": self.language,
            "failure_code": self.failure_code,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "cost_usd": self.cost_usd,
            "transcript": self.transcript,
        }


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def sample_calls(
    settings: Settings | Any,
    count: int = 10,
    days: int = 7,
    seed: int | None = None,
    language: str | None = None,
    only_failures: bool = False,
) -> list[ReviewCall]:
    """A deterministic sample of terminated calls, transcripts included.

    Deterministic because a review is an argument about evidence: "the sample
    was random" is not a defensible answer when two people reach different
    conclusions from different ten calls.
    """
    where = ["ended_at IS NOT NULL", "started_at >= ?"]
    params: list[Any] = [_cutoff(days)]
    if language:
        where.append("language LIKE ?")
        params.append(f"{language}%")
    if only_failures:
        where.append("failure_code NOT IN ('none', '')")

    sql = (
        "SELECT call_sid, direction, started_at, ended_at, language, failure_code, from_number, to_number "
        f"FROM voice_calls WHERE {' AND '.join(where)} ORDER BY started_at DESC"
    )

    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            rows = list(await conn.execute_fetchall(sql, params))
    except aiosqlite.OperationalError:
        return []
    except Exception:
        logger.exception("Spot-check sampling failed")
        return []

    if not rows:
        return []

    picked = list(rows)
    if len(picked) > count:
        # Sorted by call_sid before sampling so the seed maps to the same calls
        # regardless of the row order SQLite happens to return.
        picked = sorted(picked, key=lambda r: str(r["call_sid"]))
        picked = random.Random(seed if seed is not None else 0).sample(picked, count)
        picked.sort(key=lambda r: str(r["started_at"]), reverse=True)

    calls: list[ReviewCall] = []
    from pincer.observability.call_costs import get_call_costs

    costs = await get_call_costs(settings, [str(r["call_sid"]) for r in picked])

    for row in picked:
        call_sid = str(row["call_sid"])
        calls.append(
            ReviewCall(
                call_sid=call_sid,
                direction=str(row["direction"] or ""),
                started_at=str(row["started_at"] or ""),
                duration_seconds=_duration(str(row["started_at"] or ""), row["ended_at"]),
                language=str(row["language"] or ""),
                failure_code=str(row["failure_code"] or ""),
                from_number=mask_phone_number(str(row["from_number"] or "")),
                to_number=mask_phone_number(str(row["to_number"] or "")),
                cost_usd=costs.get(call_sid),
                transcript=await _transcript(settings, call_sid),
            )
        )
    return calls


def _duration(started_at: str, ended_at: Any) -> int:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(str(ended_at))
    except (ValueError, TypeError):
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds()))


async def _transcript(settings: Settings | Any, call_sid: str) -> list[dict[str, str]]:
    """Final transcript lines, PII-masked."""
    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await conn.execute_fetchall(
                "SELECT speaker, text, state FROM call_transcripts "
                "WHERE call_id = ? AND is_final = 1 ORDER BY timestamp ASC, id ASC",
                (call_sid,),
            )
    except Exception:
        return []
    return [
        {"speaker": str(r["speaker"] or ""), "text": mask_pii(str(r["text"] or "")), "state": str(r["state"] or "")}
        for r in rows
    ]


# ── Spot-check sheet ─────────────────────────────────────────────────

REVIEW_QUESTIONS = (
    ("language_quality", "Did the agent's language sound natural to a native speaker? (1–5)"),
    ("asr_accuracy", "Were dates, times, and names heard correctly? Note every miss verbatim."),
    ("negotiation", "Did it stay inside the offered slots and defer cleanly on counter-proposals?"),
    ("disclosure", "Was the AI disclosure audible and early?"),
    ("would_ship", "Would you be comfortable with this call going to your own customer? (y/n)"),
)


def render_spot_check(calls: list[ReviewCall], week: str = "") -> str:
    """The weekly review sheet — transcripts plus the questions to answer.

    Includes `asr_accuracy` with "note every miss verbatim" because the German
    WER on dates and names is the single most likely reality-gap (T10.3), and
    a rating without the actual misheard string cannot be turned into a fix.
    """
    if not calls:
        return "# Spot-check\n\nNo calls in the selected window.\n"

    lines = [
        f"# Voice spot-check{f' — {week}' if week else ''}",
        "",
        f"{len(calls)} call(s). Transcripts are PII-masked; numbers are shortened to country code + last two.",
        "",
        "## Summary",
        "",
        "| Call | When | Lang | Dur | Outcome | Cost |",
        "|---|---|---|---|---|---|",
    ]
    for call in calls:
        outcome = "completed" if call.failure_code in ("none", "") else call.failure_code
        cost = f"${call.cost_usd:.3f}" if call.cost_usd is not None else "—"
        lines.append(
            f"| `{call.call_sid}` | {call.started_at[:16]} | {call.language or '?'} | "
            f"{call.duration_seconds}s | {outcome} | {cost} |"
        )

    for index, call in enumerate(calls, start=1):
        lines.extend(
            [
                "",
                "---",
                "",
                f"## {index}. `{call.call_sid}` — {call.direction}, {call.language or '?'}, {call.duration_seconds}s",
                "",
                f"**Outcome:** {describe(call.failure_code) if call.failure_code else 'unknown'}  ",
                f"**Parties:** {call.from_number} → {call.to_number}",
                "",
            ]
        )
        if call.transcript:
            lines.append("```")
            for line in call.transcript:
                marker = "AGENT" if line["speaker"] == "agent" else line["speaker"].upper()
                suffix = "  ⟵ UNDELIVERED" if line["state"] == "undelivered" else ""
                lines.append(f"{marker:>8}: {line['text']}{suffix}")
            lines.append("```")
        else:
            lines.append("_No transcript stored (call may have ended before any conversation)._")

        lines.extend(["", "**Review:**", ""])
        for key, question in REVIEW_QUESTIONS:
            lines.append(f"- [ ] `{key}` — {question}")
            lines.append("      ")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## What to do with the answers",
            "",
            "- Every `asr_accuracy` miss → a fixture (`pincer pilot export-fixture <call_sid>`) and, if it "
            "repeats, a Deepgram parameter or provider A/B behind `STTProvider`.",
            "- Every `negotiation` failure → a harness persona, then a prompt fix. A prompt fix without a "
            "persona regresses silently.",
            "- Any `would_ship = n` → an issue, triaged within 24h like every other pilot bug.",
            "",
        ]
    )
    return "\n".join(lines)


# ── Harness fixtures from real transcripts (T10.3) ───────────────────

# Names are the residual PII risk: `mask_pii` handles numbers, cards, and
# emails, but a callee saying "this is Frau Schneider" is not a pattern. The
# export flags likely names for a human to confirm rather than silently
# shipping them into a committed fixture.
_LIKELY_NAME = re.compile(
    r"\b(?:Herr|Frau|Dr|Mr|Mrs|Ms|Miss)\.?\s+([A-ZÄÖÜ][a-zäöüß]+)"
    r"|\b(?:this is|my name is|hier ist|mein Name ist|ich bin)\s+([A-ZÄÖÜ][a-zäöüß]+)",
)


def detect_name_risks(lines: list[dict[str, str]]) -> list[str]:
    """Strings a human should look at before committing a fixture."""
    found: list[str] = []
    for line in lines:
        for match in _LIKELY_NAME.finditer(line.get("text", "")):
            name = next((g for g in match.groups() if g), "")
            if name and name not in found:
                found.append(name)
    return found


async def export_persona_fixture(
    settings: Settings | Any,
    call_sid: str,
    name: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Turn a real call into a replayable harness persona fixture.

    Only the *callee's* turns are kept as the script: a persona replays what the
    other party said, and the agent's side is what we are testing. Agent turns
    are preserved separately as `agent_context` so a reviewer can see the
    conversation the callee was reacting to.
    """
    transcript = await _transcript(settings, call_sid)
    if not transcript:
        raise ValueError(f"No stored transcript for {call_sid}")

    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT direction, language, failure_code FROM voice_calls WHERE call_sid = ?", (call_sid,)
            )
            row = await cursor.fetchone()
    except Exception:
        row = None

    callee_turns = [line["text"] for line in transcript if line["speaker"] != "agent" and line["text"].strip()]
    if not callee_turns:
        raise ValueError(f"{call_sid} has no callee turns — nothing for a persona to say")

    return {
        "name": name or f"real_{call_sid[-8:].lower()}",
        "source_call_sid": call_sid,
        "exported_at": datetime.now(UTC).isoformat(),
        "language": str(row["language"] or "") if row else "",
        "direction": str(row["direction"] or "") if row else "",
        "failure_code": str(row["failure_code"] or "") if row else "",
        "notes": notes,
        "opening": callee_turns[0],
        "script": callee_turns[1:],
        "agent_context": [line["text"] for line in transcript if line["speaker"] == "agent"],
        # Not a guarantee — a reviewer must clear this before committing.
        "review_required": {
            "possible_names": detect_name_risks(transcript),
            "instruction": (
                "mask_pii has removed numbers, cards, and emails. Personal NAMES are not a pattern and are "
                "listed above if detected. Replace them with placeholders before committing this fixture."
            ),
        },
    }


def fixture_to_json(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=2, ensure_ascii=False)
