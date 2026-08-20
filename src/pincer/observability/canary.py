"""
Synthetic canary call (Sprint 9, T9.2).

Every 6 hours, place a real call to a staging responder and confirm the whole
chain still works: Twilio dials, the WebSocket upgrades, STT transcribes, the
LLM answers, TTS speaks. Failure pages.

Why this exists when we already have unit tests and alerts: the four things
most likely to break a voice product are all *outside* the codebase — Twilio,
Deepgram, ElevenLabs, and the LLM provider. None of them is covered by CI, and
the golden signals only notice once real customer calls have already failed.
The canary is the one probe that fails *first*, on our own traffic, at 03:00 on
a Sunday when nobody is calling.

Safety rails, because this places real phone calls automatically:

* It refuses to run without `voice_canary_number` set, and refuses to dial a
  number on the do-not-call list.
* It runs through `make_phone_call`, so the Sprint 8 abuse gate applies —
  including quiet hours. A canary at 03:00 would otherwise be exactly the
  robocall behaviour that gate exists to prevent, so the canary user is expected
  to be listed in `voice_quiet_hours_override_users`; without that, quiet-hours
  runs are skipped rather than forced, and skipping is reported, not silent.
* Its own calls are excluded from the customer-facing golden signals by the
  `canary` direction tag, so a canary failure never distorts the call success
  rate that describes customer experience.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

CANARY_USER_ID = "pincer-canary"
CANARY_PURPOSE = "Automated health check — please confirm you can hear this and say a short sentence back."

_CALL_SID_RE = re.compile(r"Call SID: (\S+)")

# How often the canary polls the engine while waiting for the call to progress.
_POLL_INTERVAL_S = 2.0

# Canary history is what the availability SLO is inferred from (T9.5), so runs
# are persisted rather than only counted in a metric — the SLO must be
# reconstructable after a restart or a metrics-backend outage.
CANARY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS canary_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    turns INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_canary_runs_ran_at ON canary_runs(ran_at);
"""


async def _persist_run(settings: Settings | Any, result: CanaryResult) -> None:
    """Record the run. Skipped runs are stored too — a gap in coverage is a
    fact the availability SLO needs, not something to hide."""
    import aiosqlite

    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            await conn.executescript(CANARY_TABLE_SQL)
            await conn.execute(
                "INSERT INTO canary_runs (ran_at, ok, skipped, reason, call_sid, turns, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    int(result.ok),
                    int(result.skipped),
                    result.reason[:300],
                    result.call_sid,
                    result.turns,
                    round(result.duration_s, 2),
                ),
            )
            await conn.commit()
    except Exception:
        logger.exception("Failed to persist canary run")


async def recent_runs(settings: Settings | Any, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent canary runs, newest first — for the CLI and the API."""
    import aiosqlite

    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(CANARY_TABLE_SQL)
            rows = await conn.execute_fetchall(
                "SELECT ran_at, ok, skipped, reason, call_sid, turns, duration_s "
                "FROM canary_runs ORDER BY ran_at DESC LIMIT ?",
                (limit,),
            )
    except Exception:
        logger.debug("canary history query failed", exc_info=True)
        return []
    return [dict(r) for r in rows]


@dataclass
class CanaryResult:
    ok: bool
    reason: str = ""
    call_sid: str = ""
    duration_s: float = 0.0
    turns: int = 0
    skipped: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        if self.skipped:
            return f"🐤 Canary skipped: {self.reason}"
        if self.ok:
            return f"🐤 Canary OK — {self.turns} turn(s) in {self.duration_s:.0f}s (call {self.call_sid})"
        return f"🚨 *Canary FAILED*: {self.reason}\nCall: {self.call_sid or '(never placed)'}"


async def run_canary(settings: Settings | Any) -> CanaryResult:
    """Place one synthetic call and verify the media path actually worked.

    "Worked" is deliberately stricter than "Twilio accepted the request": the
    run only counts as healthy once the callee's speech has been transcribed
    and answered `voice_canary_min_turns` times. A call that connects and then
    sits in silence is the exact shape of a broken STT or TTS provider, and it
    must not read as green.
    """
    if not getattr(settings, "voice_canary_enabled", False):
        return CanaryResult(ok=True, skipped=True, reason="canary disabled")

    target = str(getattr(settings, "voice_canary_number", "") or "").strip()
    if not target:
        return CanaryResult(ok=False, reason="PINCER_VOICE_CANARY_NUMBER is not set")

    from pincer.voice.safety_gates import check_outbound_allowed, is_do_not_call

    if await is_do_not_call(settings, target):
        return CanaryResult(ok=False, reason=f"canary target {target} is on the do-not-call list")

    decision = await check_outbound_allowed(settings, target, user_id=CANARY_USER_ID)
    if not decision.allowed:
        # Quiet hours are a policy decision, not a fault — report the skip so
        # a gap in canary coverage is visible rather than looking like silence.
        return CanaryResult(
            ok=True,
            skipped=True,
            reason=f"blocked by the outbound gate ({decision.reason})",
            detail={"gate_reason": str(decision.reason)},
        )

    timeout_s = float(getattr(settings, "voice_canary_timeout_s", 180))
    min_turns = int(getattr(settings, "voice_canary_min_turns", 1))
    started = time.monotonic()

    from pincer.voice.outbound import make_phone_call

    result = await make_phone_call(
        target_number=target,
        purpose=CANARY_PURPOSE,
        instructions="This is an automated health check. Greet, ask for one short confirmation, then say goodbye.",
        max_duration=min(120, int(timeout_s)),
        context={"user_id": CANARY_USER_ID, "channel": "canary"},
    )
    if result.startswith("Error"):
        return _finish(settings, CanaryResult(ok=False, reason=result.removeprefix("Error:").strip()), started)

    match = _CALL_SID_RE.search(result)
    call_sid = match.group(1) if match else ""
    if not call_sid:
        return _finish(settings, CanaryResult(ok=False, reason="call placed but no Call SID returned"), started)

    outcome = await _await_call(settings, call_sid, timeout_s, min_turns)
    outcome.call_sid = call_sid
    return _finish(settings, outcome, started)


async def _await_call(settings: Settings | Any, call_sid: str, timeout_s: float, min_turns: int) -> CanaryResult:
    """Wait for the call to converse, then end. Returns the health verdict."""
    from pincer.voice.twiml_server import get_engine

    engine = get_engine()
    if engine is None:
        return CanaryResult(ok=False, reason="voice engine not running — cannot observe the canary call")

    deadline = time.monotonic() + timeout_s
    connected = False
    turns = 0

    while time.monotonic() < deadline:
        state = engine.get_call_state(call_sid)
        if state is None:
            # Either it never started, or it already ended. Distinguish by
            # whether we ever saw it: a call that ends after conversing is the
            # healthy path.
            if connected:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        connected = True
        turns = _observed_turns(engine, call_sid)
        if turns >= min_turns:
            # Proven healthy — hang up rather than burning the full timeout.
            with contextlib.suppress(Exception):
                await engine.end_call(call_sid)
            return CanaryResult(ok=True, reason="", turns=turns, detail={"ended_by": "canary"})
        await asyncio.sleep(_POLL_INTERVAL_S)

    if not connected:
        return CanaryResult(ok=False, reason=f"call never connected within {timeout_s:.0f}s", turns=0)
    if turns < min_turns:
        return CanaryResult(
            ok=False,
            reason=f"call connected but only {turns}/{min_turns} turn(s) completed — "
            "STT, the LLM, or TTS is not responding",
            turns=turns,
        )
    return CanaryResult(ok=True, turns=turns)


def _observed_turns(engine: Any, call_sid: str) -> int:
    """Completed turns for this call, read from the channel's metrics registry.

    The channel owns the registry and hands it to the engine
    (`VoiceChannel.set_engine`), so this is the live count of caller-utterance
    -> agent-speech round trips — precisely what proves STT and TTS both worked.
    """
    try:
        registry = getattr(engine, "metrics_registry", None)
        metrics = registry.get(call_sid) if registry is not None else None
        if metrics is not None:
            return len(metrics.turn_latencies_s)
    except Exception:  # pragma: no cover
        logger.debug("Canary turn observation failed", exc_info=True)
    return 0


def _finish(settings: Settings | Any, result: CanaryResult, started: float) -> CanaryResult:
    """Stamp duration, emit the metric, log at the right level."""
    result.duration_s = time.monotonic() - started
    if not result.skipped:
        from pincer.observability.metrics import record_canary_run

        record_canary_run(ok=result.ok, reason=result.reason[:80], duration_s=result.duration_s)

    if result.skipped:
        logger.info("Voice canary skipped: %s", result.reason)
    elif result.ok:
        logger.info("Voice canary OK in %.0fs (%d turns, call %s)", result.duration_s, result.turns, result.call_sid)
    else:
        logger.error("Voice canary FAILED after %.0fs: %s", result.duration_s, result.reason)
    return result


async def run_and_alert(settings: Settings | Any) -> CanaryResult:
    """Run the canary, persist the run, and page on failure. The scheduler calls this."""
    try:
        result = await run_canary(settings)
    except Exception as e:
        logger.exception("Voice canary crashed")
        result = CanaryResult(ok=False, reason=f"canary crashed: {e}")

    await _persist_run(settings, result)

    if result.ok:
        return result

    from pincer.observability.alerts import Alert, Severity, deliver

    await deliver(
        settings,
        [
            Alert(
                rule="canary_failed",
                severity=Severity.PAGE,
                title="Synthetic canary call failed",
                detail=result.reason,
                runbook="docs/operations/runbook.md#provider-outage",
                context={"call_sid": result.call_sid, "turns": result.turns},
            )
        ],
    )
    return result


def make_canary_handler(settings: Settings | Any) -> Any:
    """Build the CronScheduler action handler for ``voice_canary``."""

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        result = await run_and_alert(settings)
        # Only surface skips and failures to the ops channel; a green canary
        # four times a day trains people to ignore the channel.
        return None if (result.ok and not result.skipped) else result.render()

    return _handler
