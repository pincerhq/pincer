"""
Call state machine — deterministic phases with LLM-powered natural language.

Every call follows a structured flow: greeting -> intent -> verify -> execute
-> confirm -> end. The LLM handles conversation within each state, but
transitions are deterministic to ensure safety and predictability.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)


class CallPhase(StrEnum):
    RINGING = "ringing"
    GREETING = "greeting"
    INTENT_CAPTURE = "intent_capture"
    FREEFORM = "freeform"
    VERIFY = "verify"
    EXECUTE = "execute"
    CONFIRM = "confirm"
    ERROR_RECOVERY = "error_recovery"
    ENDING = "ending"
    COMPLETED = "completed"
    FAILED = "failed"

    # Outbound-specific phases
    OUTBOUND_GREETING = "outbound_greeting"
    IVR_NAVIGATION = "ivr_navigation"
    ON_HOLD = "on_hold"


# Spoken, polite exit for every phase timeout — a timeout must never be silence
# (Sprint 1, T1.2). The message plays, then the call is ended gracefully.
# Canonical text lives in pincer/voice/prompts/{en,de}.py (Sprint 2 i18n);
# this module-level dict remains the English view for backward compatibility.
from pincer.voice.prompts import get_prompt as _get_prompt  # noqa: E402

PHASE_TIMEOUT_MESSAGES: dict[CallPhase, str] = {
    CallPhase(key): value for key, value in _get_prompt("PHASE_TIMEOUT_MESSAGES", "en").items()
}

DEFAULT_TIMEOUT_MESSAGE: str = _get_prompt("DEFAULT_TIMEOUT_MESSAGE", "en")

PHASE_TIMEOUTS: dict[CallPhase, int] = {
    CallPhase.RINGING: 30,
    CallPhase.GREETING: 15,
    CallPhase.INTENT_CAPTURE: 120,
    CallPhase.FREEFORM: 300,
    CallPhase.VERIFY: 60,
    CallPhase.EXECUTE: 30,
    CallPhase.CONFIRM: 30,
    CallPhase.ERROR_RECOVERY: 30,
    CallPhase.ENDING: 15,
    CallPhase.OUTBOUND_GREETING: 30,
    CallPhase.IVR_NAVIGATION: 120,
    CallPhase.ON_HOLD: 300,
}

VALID_TRANSITIONS: dict[CallPhase, set[CallPhase]] = {
    CallPhase.RINGING: {CallPhase.GREETING, CallPhase.OUTBOUND_GREETING, CallPhase.FAILED},
    CallPhase.GREETING: {CallPhase.INTENT_CAPTURE, CallPhase.FAILED},
    CallPhase.INTENT_CAPTURE: {
        CallPhase.VERIFY,
        CallPhase.EXECUTE,
        CallPhase.FREEFORM,
        CallPhase.ENDING,
        CallPhase.ERROR_RECOVERY,
    },
    CallPhase.FREEFORM: {
        CallPhase.INTENT_CAPTURE,
        CallPhase.ENDING,
        CallPhase.VERIFY,
        CallPhase.ERROR_RECOVERY,
    },
    CallPhase.VERIFY: {
        CallPhase.EXECUTE,
        CallPhase.INTENT_CAPTURE,
        CallPhase.ERROR_RECOVERY,
        CallPhase.ENDING,
    },
    CallPhase.EXECUTE: {
        CallPhase.CONFIRM,
        CallPhase.ERROR_RECOVERY,
        CallPhase.ENDING,
    },
    CallPhase.CONFIRM: {
        CallPhase.INTENT_CAPTURE,
        CallPhase.ENDING,
        CallPhase.ERROR_RECOVERY,
    },
    CallPhase.ERROR_RECOVERY: {
        CallPhase.INTENT_CAPTURE,
        CallPhase.ENDING,
        CallPhase.FAILED,
    },
    CallPhase.ENDING: {CallPhase.COMPLETED},
    CallPhase.OUTBOUND_GREETING: {
        CallPhase.IVR_NAVIGATION,
        CallPhase.INTENT_CAPTURE,
        CallPhase.FREEFORM,
        CallPhase.FAILED,
    },
    CallPhase.IVR_NAVIGATION: {
        CallPhase.ON_HOLD,
        CallPhase.INTENT_CAPTURE,
        CallPhase.FREEFORM,
        CallPhase.FAILED,
        CallPhase.ERROR_RECOVERY,
    },
    CallPhase.ON_HOLD: {
        CallPhase.INTENT_CAPTURE,
        CallPhase.FREEFORM,
        CallPhase.FAILED,
        CallPhase.ERROR_RECOVERY,
    },
    CallPhase.COMPLETED: set(),
    CallPhase.FAILED: set(),
}


@dataclass
class PhaseTransition:
    from_phase: CallPhase
    to_phase: CallPhase
    reason: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class PendingAction:
    """An action awaiting user confirmation in the VERIFY state."""

    tool_name: str
    arguments: dict[str, Any]
    description: str
    confirmed: bool | None = None


@dataclass
class CallMachineState:
    """Serializable state for crash recovery."""

    call_sid: str
    phase: CallPhase = CallPhase.RINGING
    transitions: list[PhaseTransition] = field(default_factory=list)
    phase_entered_at: float = field(default_factory=time.monotonic)
    pending_action: PendingAction | None = None
    error_count: int = 0
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "phase": self.phase.value,
            "transitions": [
                {
                    "from": t.from_phase.value,
                    "to": t.to_phase.value,
                    "reason": t.reason,
                }
                for t in self.transitions
            ],
            "error_count": self.error_count,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallMachineState:
        state = cls(
            call_sid=data["call_sid"],
            phase=CallPhase(data["phase"]),
            error_count=data.get("error_count", 0),
            context=data.get("context", {}),
        )
        return state


class CallStateMachine:
    """Manages the lifecycle of a single voice call through deterministic phases."""

    MAX_ERRORS = 3

    def __init__(self, call_sid: str, is_outbound: bool = False) -> None:
        initial = CallPhase.RINGING
        self._state = CallMachineState(call_sid=call_sid, phase=initial)
        self._is_outbound = is_outbound
        self._timeout_task: asyncio.Task | None = None

    @property
    def phase(self) -> CallPhase:
        return self._state.phase

    @property
    def call_sid(self) -> str:
        return self._state.call_sid

    @property
    def state(self) -> CallMachineState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state.phase in (CallPhase.COMPLETED, CallPhase.FAILED)

    def transition(self, to_phase: CallPhase, reason: str = "") -> bool:
        """Attempt a state transition. Returns True if successful."""
        current = self._state.phase

        if to_phase not in VALID_TRANSITIONS.get(current, set()):
            logger.warning(
                "Invalid transition [%s]: %s -> %s (reason: %s)",
                self._state.call_sid,
                current,
                to_phase,
                reason,
            )
            return False

        transition = PhaseTransition(
            from_phase=current,
            to_phase=to_phase,
            reason=reason,
        )
        self._state.transitions.append(transition)
        self._state.phase = to_phase
        self._state.phase_entered_at = time.monotonic()

        if to_phase != CallPhase.EXECUTE:
            self._state.pending_action = None

        logger.info(
            "State transition [%s]: %s -> %s (%s)",
            self._state.call_sid,
            current,
            to_phase,
            reason,
        )
        return True

    def check_timeout(self) -> bool:
        """Check if current phase has timed out."""
        timeout = PHASE_TIMEOUTS.get(self._state.phase, 60)
        elapsed = time.monotonic() - self._state.phase_entered_at
        return elapsed > timeout

    def get_timeout_remaining(self) -> float:
        timeout = PHASE_TIMEOUTS.get(self._state.phase, 60)
        elapsed = time.monotonic() - self._state.phase_entered_at
        return max(0.0, timeout - elapsed)

    def set_pending_action(self, tool_name: str, arguments: dict, description: str) -> None:
        self._state.pending_action = PendingAction(
            tool_name=tool_name,
            arguments=arguments,
            description=description,
        )

    def confirm_action(self) -> PendingAction | None:
        if self._state.pending_action:
            self._state.pending_action.confirmed = True
            return self._state.pending_action
        return None

    def reject_action(self) -> PendingAction | None:
        if self._state.pending_action:
            self._state.pending_action.confirmed = False
            return self._state.pending_action
        return None

    def record_error(self) -> bool:
        """Record an error. Returns True if max errors exceeded."""
        self._state.error_count += 1
        return self._state.error_count >= self.MAX_ERRORS

    def start_call(self) -> None:
        """Transition from RINGING to the appropriate greeting state."""
        if self._is_outbound:
            self.transition(CallPhase.OUTBOUND_GREETING, "call_answered")
        else:
            self.transition(CallPhase.GREETING, "call_answered")

    def get_phase_instruction(self, language: str = "en") -> str:
        """Behavioral instruction for the current phase, in the call language."""
        instructions = _get_prompt("PHASE_INSTRUCTIONS", language) or {}
        return instructions.get(self._state.phase.value, "")

    def get_timeout_message(self, language: str = "en", formality: str = "sie") -> str:
        """Spoken, polite exit line for the current phase timing out."""
        messages = _get_prompt("PHASE_TIMEOUT_MESSAGES", language, formality) or {}
        default = _get_prompt("DEFAULT_TIMEOUT_MESSAGE", language, formality) or DEFAULT_TIMEOUT_MESSAGE
        return messages.get(self._state.phase.value, default)

    def force_terminal(self, phase: CallPhase = CallPhase.FAILED, reason: str = "forced") -> None:
        """Force the machine into a terminal state, bypassing transition rules.

        Cleanup paths (hangup, provider failure, watchdog) must always leave
        the machine in COMPLETED or FAILED — never a stuck intermediate phase.
        The forced jump is still recorded in the transition history.
        """
        if phase not in (CallPhase.COMPLETED, CallPhase.FAILED):
            phase = CallPhase.FAILED
        if self.is_terminal:
            return
        current = self._state.phase
        self._state.transitions.append(PhaseTransition(from_phase=current, to_phase=phase, reason=reason))
        self._state.phase = phase
        self._state.phase_entered_at = time.monotonic()
        self._state.pending_action = None
        logger.info("State forced terminal [%s]: %s -> %s (%s)", self._state.call_sid, current, phase, reason)


# English view of the localized phase instructions (canonical text in prompts/en.py)
_PHASE_INSTRUCTIONS: dict[CallPhase, str] = {
    CallPhase(key): value for key, value in _get_prompt("PHASE_INSTRUCTIONS", "en").items()
}
