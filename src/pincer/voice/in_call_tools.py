"""
In-call tool gate (Sprint 11) — the runtime around ``tool_policy.decide``.

One gate per live call. The agent loop hands every LLM tool_use in call
context to ``gate.run(...)``; the gate decides (§5), speaks the deterministic
lines (§6/§8), executes under the per-tool timeout, renders the result for
speech (§7), records the CallAction (§9), and tells the channel whether the
LLM's remaining text this turn should be suppressed (because the gate already
spoke the outcome).

Flows (§6):
- Tier R: optional filler → execute → ``[TOOL RESULT: …]`` into the LLM context.
- Tier W / verbal: speak VERIFY_ACTION, park the pending write; the next caller
  utterance is parsed (yes/no/unclear), a YES sets the fingerprinted verbal
  confirmation and the LLM is told to re-emit the tool call, which then
  executes.
- Tier W / user: speak TOOL_HOLD, post the approval card to the initiating
  user, reassure every 8s, then approved → execute / denied → TOOL_DECLINED /
  timeout → TOOL_TIMEOUT_DEFER + post-call follow-up.
- Tier W / off: execute immediately (budgeted), disclosed in the report.

The gate is bound to the turn through a ContextVar (``bind_gate``) so both
agent paths — streaming and blocking — find it without new plumbing, exactly
like the Sprint 9 call-cost binding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pincer.voice import approvals, tool_policy
from pincer.voice.prompts import get_prompt
from pincer.voice.safety_gates import ConfirmationStatus, parse_confirmation
from pincer.voice.state_machine import CallPhase
from pincer.voice.tool_speech import describe_action, render, render_denied, render_pending
from pincer.voice.transcript import Speaker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from pincer.voice.engine import CallState, VoiceEngine
    from pincer.voice.state_machine import CallStateMachine
    from pincer.voice.transcript import CallAction, TranscriptLogger

logger = logging.getLogger(__name__)

# §6.3: while the initiating user is deciding, the callee hears this every N s.
HOLD_REASSURE_INTERVAL_S = 8.0

# §6.1: per-tool static latency hints. Anything expected to exceed the
# threshold gets a spoken filler FIRST (only when nothing was spoken yet this
# turn — a model that already said "let me check" needs no second filler).
FILLER_THRESHOLD_S = 1.5
TOOL_LATENCY_HINTS_S: dict[str, float] = {
    "google__check_freebusy": 2.0,
    "google__list_events": 2.0,
    "google__create_event": 2.5,
    "google__update_event": 2.5,
    "memory_search": 1.0,
    "business_profile_lookup": 0.5,
    "contact_lookup": 0.3,
    "send_owner_message": 0.5,
    "memory_note": 0.5,
}

# Deny reason used when the callee hangs up while a `user` approval is open
# (no execution, card edited to "call ended"). Not in §5.2's list because
# nothing was refused — it is recorded so the row explains itself.
REASON_CALL_ENDED = "call_ended"

META_DEFERRED = "deferred_actions"

_current_gate: ContextVar[InCallToolGate | None] = ContextVar("pincer_in_call_tool_gate", default=None)


@contextmanager
def bind_gate(gate: InCallToolGate | None) -> Iterator[None]:
    """Bind the call's gate to the current task context (inherited by tasks
    created inside the block — the streaming turn task included)."""
    token = _current_gate.set(gate)
    try:
        yield
    finally:
        _current_gate.reset(token)


def current_gate() -> InCallToolGate | None:
    return _current_gate.get()


@dataclass
class GateResult:
    """What the agent loop feeds back to the LLM for one tool_use."""

    content: str
    is_error: bool = False
    executed: bool = False
    decision: tool_policy.ToolDecision | None = None
    deny_reason: str = ""
    # The gate already spoke the outcome (verify question, hold, declined,
    # deferred, error) — the channel must not speak the LLM's follow-up text.
    suppress_llm_speech: bool = False


@dataclass
class PendingWrite:
    tool_name: str
    arguments: dict[str, Any]
    description: str
    fingerprint: str
    asked_turn: int
    reasks: int = 0
    confirm_action: CallAction | None = field(default=None, repr=False)


@dataclass
class CallerVerdict:
    """Outcome of parsing the caller's utterance while a verify is pending."""

    status: str = "none"  # none | confirmed | rejected | unclear
    handled: bool = False  # the gate spoke (re-ask); no LLM turn needed
    system_note: str = ""  # extra system instruction for this LLM turn


class InCallToolGate:
    """Per-call runtime for in-call tool execution."""

    def __init__(
        self,
        *,
        call_sid: str,
        state: CallState,
        sm: CallStateMachine,
        settings: Any,
        transcript: TranscriptLogger | None = None,
        engine: VoiceEngine | None = None,
        approval_target: tuple[str, str] | None = None,
        allowed_tools: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.state = state
        self.sm = sm
        self.settings = settings
        self.transcript = transcript
        self.engine = engine
        self.approval_target = approval_target  # (user_id, channel) of the initiating user
        if allowed_tools is not None:
            state.metadata[tool_policy.META_ALLOWED_TOOLS] = frozenset(allowed_tools)
        state.metadata.setdefault(tool_policy.META_WRITES_USED, 0)
        self.pending: PendingWrite | None = None
        self.suppress_llm_speech = False
        self._speaker: Callable[[str], Awaitable[Any]] | None = None
        self._spoken_any: Callable[[], bool] | None = None
        self._verify_spoken_turn = -1
        self._filler_spoken_turn = -1
        self._approval_request: approvals.VoiceApprovalRequest | None = None
        self.executed_count = 0

    # ── Turn plumbing ────────────────────────────────────

    @property
    def turn_no(self) -> int:
        return int(self.state.metadata.get(tool_policy.META_TURN_NO, 0) or 0)

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(self.state.metadata.get(tool_policy.META_ALLOWED_TOOLS) or ())

    @property
    def writes_used(self) -> int:
        return int(self.state.metadata.get(tool_policy.META_WRITES_USED, 0) or 0)

    def begin_turn(self) -> int:
        """Called by the channel on every caller utterance."""
        self.suppress_llm_speech = False
        return tool_policy.begin_turn(self.state)

    def set_speaker(
        self,
        speak: Callable[[str], Awaitable[Any]] | None,
        spoken_any: Callable[[], bool] | None = None,
    ) -> None:
        """The channel's per-turn speech path (keeps CR stream state and the
        transcript in sync). Without one, the gate speaks via the engine."""
        self._speaker = speak
        self._spoken_any = spoken_any

    def filter_schemas(self, tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The EXACT schema set for this call (§4 rule)."""
        return tool_policy.call_context_schemas(tool_schemas, self.allowed_tools)

    def language(self) -> tuple[str, str]:
        from pincer.voice.language import de_formality

        return str(self.state.language or "en")[:2], de_formality(self.settings)

    def prompt(self, key: str) -> str:
        lang, formality = self.language()
        return str(get_prompt(key, lang, formality) or "")

    async def speak(self, text: str) -> None:
        """Deterministic gate speech: through the channel's turn speaker when
        one is set (transcript + CR bookkeeping there), else engine-direct."""
        if not text:
            return
        if self._speaker is not None:
            await self._speaker(text)
            return
        if self.transcript is not None:
            self.transcript.log_utterance(Speaker.AGENT, text, state=str(self.sm.phase))
        if self.engine is not None:
            with contextlib.suppress(Exception):
                await self.engine.send_speech(self.call_sid, text)

    def _log_action(self, action_type: str, tool_name: str, **kwargs: Any) -> CallAction | None:
        if self.transcript is None:
            return None
        return self.transcript.log_action(action_type, tool_name, **kwargs)

    def _metric(self, tool_name: str, decision: tool_policy.ToolDecision, action: str, reason: str) -> None:
        try:
            from pincer.observability.metrics import record_tool_decision

            record_tool_decision(tool=tool_name, action=action, reason=reason, tier=decision.tier, mode=decision.mode)
        except Exception:
            logger.debug("tool decision metric failed", exc_info=True)

    # ── Entry point ──────────────────────────────────────

    async def run(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        execute: Callable[[], Awaitable[tuple[str, bool]]],
    ) -> GateResult:
        """Decide and act on one LLM tool_use. ``execute`` runs the real tool
        and returns ``(content, is_error)``; it is only awaited when the policy
        says execute."""
        args = dict(arguments or {})
        decision = tool_policy.decide(tool_name, self.state, self.settings, args)
        logger.info(
            "In-call tool decision [%s]: %s -> %s (%s, tier=%s, mode=%s)",
            self.call_sid,
            tool_name,
            decision.action,
            decision.reason,
            decision.tier,
            decision.mode or "-",
        )
        if decision.action == "deny":
            return await self._deny(tool_name, args, decision, decision.reason)
        if decision.action == "need_verbal":
            return await self._start_verify(tool_name, args, decision)
        if decision.action == "need_user_approval":
            return await self._user_approval(tool_name, args, execute, decision)
        approval_mode = "auto" if decision.tier == tool_policy.TIER_R else decision.mode
        user_confirmed = True if decision.mode == "verbal" else None
        return await self._execute(tool_name, args, execute, decision, approval_mode, user_confirmed)

    # ── Deny ─────────────────────────────────────────────

    async def _deny(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: tool_policy.ToolDecision,
        reason: str,
        *,
        speak_key: str = "",
        approval_mode: str = "",
    ) -> GateResult:
        lang, _ = self.language()
        logger.warning("In-call tool denied [%s]: %s (%s)", self.call_sid, tool_name, reason)
        self._log_action(
            "tool_denied",
            tool_name,
            input_summary=tool_policy.canonical_json(args)[:500],
            output_summary=reason,
            user_confirmed=False if reason == tool_policy.REASON_APPROVAL_DENIED else None,
            tier=decision.tier,
            approval_mode=approval_mode or decision.mode,
            deny_reason=reason,
        )
        self._metric(tool_name, decision, "deny", reason)
        suppress = False
        if speak_key:
            await self.speak(self.prompt(speak_key))
            self.suppress_llm_speech = True
            suppress = True
        return GateResult(
            content=f"[TOOL DENIED: {reason}] {render_denied(lang)}",
            is_error=True,
            decision=decision,
            deny_reason=reason,
            suppress_llm_speech=suppress,
        )

    # ── Execute (R, off, confirmed verbal, approved user) ─

    async def _execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        execute: Callable[[], Awaitable[tuple[str, bool]]],
        decision: tool_policy.ToolDecision,
        approval_mode: str,
        user_confirmed: bool | None,
    ) -> GateResult:
        lang, _ = self.language()
        self._metric(tool_name, decision, "execute", decision.reason)
        # §6.1 filler: only for slow tools, only if the caller heard nothing yet
        # this turn, and at most once per turn (a read followed by a write is
        # one "moment please", not two).
        if (
            TOOL_LATENCY_HINTS_S.get(tool_name, 0.0) > FILLER_THRESHOLD_S
            and not self._already_spoken()
            and self._filler_spoken_turn != self.turn_no
        ):
            self._filler_spoken_turn = self.turn_no
            await self.speak(self.prompt("TOOL_WAIT_FILLER"))

        timeout = float(getattr(self.settings, "voice_tool_timeout_s", 10) or 10)
        started = time.monotonic()
        try:
            content, is_error = await asyncio.wait_for(execute(), timeout=timeout)
        except TimeoutError:
            logger.warning("In-call tool %s timed out after %.0fs [%s]", tool_name, timeout, self.call_sid)
            return await self._defer(tool_name, args, decision, tool_policy.REASON_TOOL_TIMEOUT, approval_mode)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the executor normally catches; belt and braces
            logger.exception("In-call tool %s raised [%s]", tool_name, self.call_sid)
            content, is_error = f"Error: {type(e).__name__}: {e}", True
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if is_error:
            logger.warning("In-call tool %s failed [%s]: %s", tool_name, self.call_sid, content[:200])
            result = await self._deny(
                tool_name,
                args,
                decision,
                tool_policy.REASON_TOOL_ERROR,
                speak_key="TOOL_ERROR",
                approval_mode=approval_mode,
            )
            if decision.tier == tool_policy.TIER_W:
                self._add_deferred(tool_name, args, tool_policy.REASON_TOOL_ERROR)
            return result

        spoken = render(tool_name, content, lang, args)
        if decision.tier == tool_policy.TIER_W:
            self.state.metadata[tool_policy.META_WRITES_USED] = self.writes_used + 1
            tool_policy.clear_verbal_confirmation(self.state)
            self.pending = None
            if self.sm.phase in (CallPhase.EXECUTE, CallPhase.VERIFY):
                if self.sm.phase == CallPhase.VERIFY:
                    self.sm.transition(CallPhase.EXECUTE, "tool_execute")
                self.sm.transition(CallPhase.CONFIRM, "tool_executed")
        self.executed_count += 1
        self._log_action(
            "tool_execute",
            tool_name,
            input_summary=tool_policy.canonical_json(args)[:500],
            output_summary=spoken,
            user_confirmed=user_confirmed,
            tier=decision.tier,
            approval_mode=approval_mode,
        )
        logger.info(
            "In-call tool executed [%s]: %s in %.0fms (%s)", self.call_sid, tool_name, elapsed_ms, approval_mode
        )
        return GateResult(content=f"[TOOL RESULT: {spoken}]", executed=True, decision=decision)

    def _already_spoken(self) -> bool:
        if self._spoken_any is None:
            return False
        try:
            return bool(self._spoken_any())
        except Exception:
            return False

    async def _defer(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: tool_policy.ToolDecision,
        reason: str,
        approval_mode: str,
    ) -> GateResult:
        """Timeouts: speak the deferral line, record the denial, and queue the
        action as a post-call follow-up suggestion (Sprint 3 T3.4 mechanism)."""
        result = await self._deny(
            tool_name, args, decision, reason, speak_key="TOOL_TIMEOUT_DEFER", approval_mode=approval_mode
        )
        self._add_deferred(tool_name, args, reason)
        return result

    def _add_deferred(self, tool_name: str, args: dict[str, Any], reason: str) -> None:
        lang, _ = self.language()
        deferred = self.state.metadata.setdefault(META_DEFERRED, [])
        deferred.append(
            {
                "tool": tool_name,
                "draft_args": dict(args),
                "reason": reason,
                "summary": describe_action(tool_name, args, lang),
            }
        )

    def deferred_actions(self) -> list[dict[str, Any]]:
        return list(self.state.metadata.get(META_DEFERRED) or [])

    # ── Verbal mode (§6.2) ───────────────────────────────

    async def _start_verify(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: tool_policy.ToolDecision,
    ) -> GateResult:
        lang, _ = self.language()
        description = describe_action(tool_name, args, lang)
        fingerprint = tool_policy.action_fingerprint(tool_name, args)
        self._metric(tool_name, decision, "need_verbal", decision.reason)

        already_asked = (
            self.pending is not None
            and self.pending.fingerprint == fingerprint
            and self._verify_spoken_turn == self.turn_no
        )
        if not already_asked:
            self.pending = PendingWrite(
                tool_name=tool_name,
                arguments=dict(args),
                description=description,
                fingerprint=fingerprint,
                asked_turn=self.turn_no,
            )
            self.state.metadata[tool_policy.META_PENDING_ARGS] = dict(args)
            self._enter_verify(tool_name, args, description)
            question = self.prompt("VERIFY_ACTION").format(action=description)
            await self.speak(question)
            self._verify_spoken_turn = self.turn_no
            self.pending.confirm_action = self._log_action(
                "confirm",
                tool_name,
                input_summary=description,
                user_confirmed=None,
                tier=decision.tier,
                approval_mode="verbal",
            )
        self.suppress_llm_speech = True
        return GateResult(
            content=f"[TOOL RESULT: {render_pending(description, lang)}]",
            decision=decision,
            suppress_llm_speech=True,
        )

    def _enter_verify(self, tool_name: str, args: dict[str, Any], description: str) -> None:
        """Move the state machine to VERIFY along valid transitions only (a
        write can be proposed from any conversational phase, and a re-proposal
        with changed args arrives while we are still in EXECUTE)."""
        if self.sm.phase != CallPhase.VERIFY:
            for hop in self._route_to(CallPhase.VERIFY):
                if not self.sm.transition(hop, "tool_verify"):
                    break
        self.sm.set_pending_action(tool_name, args, description)

    def _route_to(self, target: CallPhase, max_depth: int = 4) -> list[CallPhase]:
        """Shortest valid transition path from the current phase (BFS)."""
        from pincer.voice.state_machine import VALID_TRANSITIONS

        start = self.sm.phase
        if start == target:
            return []
        frontier: list[tuple[CallPhase, list[CallPhase]]] = [(start, [])]
        seen = {start}
        while frontier:
            phase, path = frontier.pop(0)
            if len(path) >= max_depth:
                continue
            for nxt in VALID_TRANSITIONS.get(phase, set()):
                if nxt in seen or nxt in (CallPhase.COMPLETED, CallPhase.FAILED):
                    continue
                if nxt == target:
                    return [*path, nxt]
                seen.add(nxt)
                frontier.append((nxt, [*path, nxt]))
        return []

    async def handle_caller_utterance(self, text: str) -> CallerVerdict:
        """Called by the channel BEFORE the LLM turn. Parses yes/no/unclear
        while a verbal confirmation is pending."""
        pending = self.pending
        if pending is None:
            return CallerVerdict()
        status = parse_confirmation(text)
        lang, _ = self.language()

        if status == ConfirmationStatus.CONFIRMED:
            tool_policy.set_verbal_confirmation(self.state, pending.tool_name, pending.arguments)
            if pending.confirm_action is not None:
                pending.confirm_action.user_confirmed = True
            self.sm.confirm_action()
            if self.sm.phase == CallPhase.VERIFY:
                self.sm.transition(CallPhase.EXECUTE, "verbal_confirmed")
            note = (
                "[CONFIRMED] The call partner just said YES to: "
                f"{pending.description}. Call the tool {pending.tool_name} NOW with exactly these arguments: "
                f"{json.dumps(pending.arguments, ensure_ascii=False)}. Do not ask again, do not change them."
            )
            logger.info("Verbal confirmation captured [%s]: %s", self.call_sid, pending.tool_name)
            return CallerVerdict(status="confirmed", system_note=note)

        if status == ConfirmationStatus.UNCLEAR and pending.reasks < 1:
            pending.reasks += 1
            question = self.prompt("VERIFY_ACTION").format(action=pending.description)
            await self.speak(self.prompt("VERIFY_REASK").format(question=question))
            return CallerVerdict(status="unclear", handled=True)

        # REJECTED, or a second UNCLEAR (= NO)
        tool_policy.clear_verbal_confirmation(self.state)
        if pending.confirm_action is not None:
            pending.confirm_action.user_confirmed = False
        self.sm.reject_action()
        if self.sm.phase == CallPhase.VERIFY:
            self.sm.transition(CallPhase.INTENT_CAPTURE, "verbal_rejected")
        self.pending = None
        why = "declined" if status == ConfirmationStatus.REJECTED else "gave no clear confirmation"
        note = (
            f"[DECLINED] The call partner {why} for: {pending.description}. Do NOT perform it and do not "
            "ask again now. Acknowledge briefly that nothing has been changed and continue with the call."
        )
        logger.info("Verbal confirmation %s [%s]: %s", why, self.call_sid, pending.tool_name)
        return CallerVerdict(status="rejected", system_note=note)

    # ── User mode (§6.3 / §6.5) ──────────────────────────

    async def _user_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        execute: Callable[[], Awaitable[tuple[str, bool]]],
        decision: tool_policy.ToolDecision,
    ) -> GateResult:
        lang, _ = self.language()
        description = describe_action(tool_name, args, lang)
        user_id, channel = self.approval_target or ("", "")
        timeout = float(getattr(self.settings, "voice_approval_timeout_s", 25) or 25)
        self._metric(tool_name, decision, "need_user_approval", decision.reason)

        await self.speak(self.prompt("TOOL_HOLD"))
        self.suppress_llm_speech = True

        req = await approvals.request(
            call_sid=self.call_sid,
            tool_name=tool_name,
            summary=description,
            language=lang,
            args_preview=args,
            user_id=user_id,
            channel=channel,
            timeout_s=timeout,
        )
        self._approval_request = req
        outcome = approvals.EXPIRED
        if req.extra.get("presented"):
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    approved = await asyncio.wait_for(
                        asyncio.shield(req.future), timeout=min(HOLD_REASSURE_INTERVAL_S, remaining)
                    )
                except TimeoutError:
                    if req.final_state:
                        outcome = req.final_state
                        break
                    await self.speak(self.prompt("TOOL_HOLD_REASSURE"))
                    continue
                except asyncio.CancelledError:
                    raise
                outcome = approvals.APPROVED if approved else (req.final_state or approvals.DENIED)
                break
        self._approval_request = None

        if outcome == approvals.APPROVED:
            await approvals.finalize(req, approvals.APPROVED)
            self.suppress_llm_speech = False  # the LLM phrases the result
            return await self._execute(tool_name, args, execute, decision, "user", True)
        if outcome == approvals.CALL_ENDED:
            await approvals.finalize(req, approvals.CALL_ENDED)
            return await self._deny(tool_name, args, decision, REASON_CALL_ENDED, approval_mode="user")
        if outcome == approvals.DENIED:
            await approvals.finalize(req, approvals.DENIED)
            return await self._deny(
                tool_name,
                args,
                decision,
                tool_policy.REASON_APPROVAL_DENIED,
                speak_key="TOOL_DECLINED",
                approval_mode="user",
            )
        await approvals.finalize(req, approvals.EXPIRED)
        return await self._defer(tool_name, args, decision, tool_policy.REASON_APPROVAL_TIMEOUT, "user")

    # ── Call end ─────────────────────────────────────────

    async def on_call_end(self) -> None:
        """Callee hung up: cancel any open approval card (no execution)."""
        with contextlib.suppress(Exception):
            await approvals.cancel_for_call(self.call_sid)
        self.pending = None
        self._speaker = None


__all__ = [
    "FILLER_THRESHOLD_S",
    "HOLD_REASSURE_INTERVAL_S",
    "META_DEFERRED",
    "REASON_CALL_ENDED",
    "TOOL_LATENCY_HINTS_S",
    "CallerVerdict",
    "GateResult",
    "InCallToolGate",
    "PendingWrite",
    "bind_gate",
    "current_gate",
]
