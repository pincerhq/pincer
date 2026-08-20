"""
ReceptionSession (Sprint 12) — the per-call controller of the inbound
receptionist.

It sits between the VoiceChannel and the LLM:

- **Before** every caller turn it decides whether the turn is handled
  deterministically (slot filling, booking negotiation, transfer, silence,
  injection/extraction deflection) or goes to the conversation LLM with a
  constrained instruction (intent classification, FAQ answer from the
  profile only).
- **After** an LLM turn it parses the ``[INTENT:…]`` token, strips it before
  TTS, and drives the §6 transition table.

Everything the caller hears from the session itself comes from the language
pack (``RECEPTIONIST_LINES``); the caller is untrusted, so the session never
reveals anything beyond the profile and free/busy *windows*. Writes go through
the Sprint 11 gate (``google__create_event`` under the configured inbound
approval mode) — the session is trusted code, but it still uses the policy.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.voice import tool_policy
from pincer.voice.prompts import get_prompt
from pincer.voice.receptionist import booking as bk
from pincer.voice.receptionist import slots as sl
from pincer.voice.receptionist.intents import (
    INTENT_AFTER_HOURS,
    INTENT_APPOINTMENT,
    INTENT_HUMAN,
    INTENT_MESSAGE,
    INTENT_QUESTION,
    parse_intent_token,
)
from pincer.voice.safety_gates import ConfirmationStatus, parse_confirmation
from pincer.voice.state_machine import CallPhase
from pincer.voice.tool_speech import spoken_datetime

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.voice.engine import CallState, VoiceEngine
    from pincer.voice.in_call_tools import InCallToolGate
    from pincer.voice.receptionist.profile import BusinessProfile
    from pincer.voice.state_machine import CallStateMachine
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

SILENCE_REPROMPT_S = 10.0
SILENCE_HANGUP_S = 10.0  # after the re-prompt
TRANSFER_DIAL_TIMEOUT_S = 20
MAX_UNKNOWN_INTENTS = 2
MAX_BOOKING_DECLINES = 3

# Deterministic tripwires — the LLM is instructed too, but a tripwire makes
# the deflection a code guarantee for the obvious cases (red-team CI).
_INJECTION_MARKERS = (
    "ignore all previous",
    "ignore your instructions",
    "ignore previous instructions",
    "developer mode",
    "system prompt",
    "list your tools",
    "list every tool",
    "you are now",
    "ignorieren sie ihre anweisungen",
    "ignoriere deine anweisungen",
    "bisherigen anweisungen",
    "entwicklermodus",
    "system-prompt",
    "ihre tools",
    "sie sind jetzt",
    "ігноруй",
    "системний промпт",
)
_EXTRACTION_MARKERS = (
    "welche termine",
    "wer hat einen termin",
    "terminplan",
    "kalender vorlesen",
    "lies mir",
    "lesen sie mir",
    "seine termine",
    "ihre termine",
    "andere patienten",
    "anderen patienten",
    "andere kunden",
    "anderen kunden",
    "namen der anderen",
    "wer hat heute",
    "wer hat morgen",
    "privatadresse",
    "handynummer von",
    "what appointments",
    "which appointments",
    "read me",
    "his schedule",
    "her schedule",
    "his calendar",
    "her calendar",
    "other patients",
    "other customers",
    "home address",
    "які зустрічі",
    "прочитай мені",
    "його розклад",
)
_GOODBYE_MARKERS = (
    "auf wiederhören",
    "tschüss",
    "tschuess",
    "das war's",
    "das wars",
    "das ist alles",
    "nein danke",
    "nein, danke",
    "goodbye",
    "bye",
    "that's all",
    "that is all",
    "no thanks",
    "no, thanks",
    "до побачення",
    "це все",
    "ні, дякую",
)
_DECLINE_EMAIL = ("nein", "no", "kein", "nicht nötig", "not needed", "ні", "keine")
_HUMAN_MARKERS = (
    "mensch",
    "mitarbeiter",
    "jemanden sprechen",
    "durchstellen",
    "verbinden sie mich",
    "human",
    "real person",
    "speak to someone",
    "transfer me",
    "людин",
    "з'єднайте",
)


@dataclass
class TurnPlan:
    """What the channel should do with this caller turn."""

    handled: bool = False  # the session spoke; no LLM turn
    system_note: str = ""  # extra system instruction for the LLM turn
    override_text: str = ""  # replace the caller text given to the LLM (mixed-request FAQ)


@dataclass
class BookingState:
    window: tuple[datetime, datetime] | None = None
    busy: list[tuple[datetime, datetime]] = field(default_factory=list)
    candidates: list[datetime] = field(default_factory=list)
    chosen: datetime | None = None
    declines: int = 0
    email_attempts: int = 0
    booked: bool = False
    calendar_link: str = ""
    event_id: str = ""
    slot_spoken: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "booked": self.booked,
            "slot": self.chosen.isoformat() if self.chosen else "",
            "slot_spoken": self.slot_spoken,
            "calendar_link": self.calendar_link,
            "event_id": self.event_id,
            "declines": self.declines,
        }


def opening_text(profile: BusinessProfile, language: str, now: datetime | None = None) -> tuple[str, bool]:
    """(the receptionist greeting incl. after-hours message + first question, is_open).

    Spoken as the call's welcome greeting (TwiML). After hours the greeting
    already announces the closure and asks for the caller's name, so the
    session starts in TAKE_MESSAGE with the name question already asked."""
    lang = str(language or profile.default_language)[:2]
    greeting = str(get_prompt("RECEPTIONIST_GREETING", lang) or "").format(business_name=profile.business.name)
    is_open = profile.is_open(now or datetime.now(UTC))
    if is_open:
        return greeting, True
    lines = get_prompt("RECEPTIONIST_LINES", lang) or {}
    closed = profile.after_hours.message.strip() or str(lines.get("after_hours_default", ""))
    # greeting minus its question, then the closure note, then the first slot question
    base = greeting.rsplit(".", 1)[0] + "." if "." in greeting else greeting
    return f"{base} {closed} {lines.get('ask_name', '')}".strip(), False


class ReceptionSession:
    """Per-call receptionist controller (see module docstring)."""

    def __init__(
        self,
        *,
        call_sid: str,
        state: CallState,
        sm: CallStateMachine,
        settings: Any,
        profile: BusinessProfile,
        gate: InCallToolGate | None = None,
        tools: Any = None,
        transcript: TranscriptLogger | None = None,
        engine: VoiceEngine | None = None,
        now_fn: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.call_sid = call_sid
        self.state = state
        self.sm = sm
        self.settings = settings
        self.profile = profile
        self.gate = gate
        self.tools = tools
        self.transcript = transcript
        self.engine = engine
        self._now_fn = now_fn or (lambda: datetime.now(profile.tz))
        self._clock = clock
        self.language = str(state.language or profile.default_language)[:2]
        self.lines: dict[str, str] = dict(get_prompt("RECEPTIONIST_LINES", self.language) or {})

        self.intent = ""
        self.unknown_intents = 0
        self.injection_attempts = 0
        self.extraction_attempts = 0
        self.slots = sl.MessageSlots()
        self.step = ""
        self.booking = BookingState()
        self.pending_faq = ""
        self.transfer_attempted = False
        self.transfer_failed = False
        self.ended = False
        self.end_reason = ""
        self.turns = 0
        self.greeted_at = clock()
        self.last_caller_at: float | None = None
        self.silence_reprompted = False
        self._reprompt_at = 0.0
        self._yes_no_retry = False
        self._identity_then = ""  # "booking" | "message"
        self.message_retries = 0
        self.is_open = profile.is_open(self._now_fn())

        state.metadata["receptionist"] = True
        state.metadata[tool_policy.META_MODE_OVERRIDES] = {
            "google__create_event": str(getattr(settings, "receptionist_booking_approval", "off") or "off"),
        }
        self._sync()

    # ── Lifecycle ─────────────────────────────────────────

    def start(self) -> None:
        """Greeting spoken (TwiML): GREETING → RECEPTION_INTENT / AFTER_HOURS(→TAKE_MESSAGE)."""
        self._metric("answered")
        if self.sm.phase != CallPhase.GREETING:
            return
        if self.is_open:
            self.sm.transition(CallPhase.RECEPTION_INTENT, "greeting_spoken")
        else:
            self.intent = INTENT_AFTER_HOURS
            self._metric("intent", intent=INTENT_AFTER_HOURS)
            self.sm.transition(CallPhase.AFTER_HOURS, "greeting_spoken_closed")
            # The after-hours greeting already asked for the name (§8.5)
            self.sm.transition(CallPhase.TAKE_MESSAGE, "after_hours_message")
            self.step = "name"
            self._identity_then = "message"
        self._sync()

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "unknown_intents": self.unknown_intents,
            "injection_attempts": self.injection_attempts,
            "slots": self.slots.to_dict(),
            "booking": self.booking.to_dict(),
            "transfer_attempted": self.transfer_attempted,
            "transfer_failed": self.transfer_failed,
            "is_open": self.is_open,
            "language": self.language,
            "turns": self.turns,
            "end_reason": self.end_reason,
        }

    def _sync(self) -> None:
        self.state.metadata["reception"] = self.to_dict()
        self.state.metadata["unknown_intents"] = self.unknown_intents

    # ── Speech / helpers ──────────────────────────────────

    def line(self, key: str, **kwargs: Any) -> str:
        template = str(self.lines.get(key) or (get_prompt("RECEPTIONIST_LINES", "en") or {}).get(key, ""))
        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, IndexError):
            return template

    async def speak(self, text: str) -> None:
        if not text:
            return
        if self.gate is not None:
            await self.gate.speak(text)
            return
        if self.transcript is not None:
            from pincer.voice.transcript import Speaker

            self.transcript.log_utterance(Speaker.AGENT, text, state=str(self.sm.phase))
        if self.engine is not None:
            with contextlib.suppress(Exception):
                await self.engine.send_speech(self.call_sid, text)

    def _metric(self, event: str, **dims: Any) -> None:
        try:
            from pincer.observability.metrics import record_inbound_event

            record_inbound_event(event, language=self.language, **dims)
        except Exception:
            logger.debug("inbound metric failed", exc_info=True)

    def _log_action(self, action_type: str, summary: str, output: str = "") -> None:
        if self.transcript is not None:
            self.transcript.log_action(action_type, "receptionist", input_summary=summary[:300], output_summary=output)

    async def _end(self, reason: str, *, completed: bool = True) -> None:
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        self._sync()
        if not self.sm.is_terminal:
            if self.sm.phase != CallPhase.ENDING:
                self.sm.transition(CallPhase.ENDING, reason)
            if self.sm.phase == CallPhase.ENDING:
                self.sm.transition(CallPhase.COMPLETED if completed else CallPhase.FAILED, reason)
            else:
                self.sm.force_terminal(CallPhase.COMPLETED if completed else CallPhase.FAILED, reason=reason)
        if self.engine is not None:
            with contextlib.suppress(Exception):
                await self.engine.end_call(self.call_sid)

    def _to_phase(self, phase: CallPhase, reason: str) -> None:
        if self.sm.phase == phase:
            return
        if not self.sm.transition(phase, reason):
            logger.warning("Receptionist: %s -> %s not allowed [%s]", self.sm.phase, phase, self.call_sid)

    @property
    def caller_id(self) -> str:
        return str(self.state.caller_number or "")

    # ── System prompt block ───────────────────────────────

    def system_block(self) -> str:
        deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", self.language) or "")
        rules = str(get_prompt("RECEPTIONIST_RULES", self.language) or "").format(
            business_name=self.profile.business.name, deflect=deflect
        )
        faq = "\n".join(f"- Q: {item.q}\n  A: {item.a}" for item in self.profile.faq) or "-"
        profile_block = str(get_prompt("RECEPTIONIST_PROFILE_BLOCK", self.language) or "").format(
            business_name=self.profile.business.name,
            hours=self.profile.speakable_hours(self.language),
            address=self.profile.address or "-",
            services=", ".join(self.profile.services) or "-",
            faq=faq,
        )
        parts = [rules, profile_block]
        if self.sm.phase == CallPhase.RECEPTION_INTENT:
            parts.append(str(get_prompt("RECEPTIONIST_INTENT_INSTRUCTION", self.language) or ""))
            parts.append(
                "If the question is NOT answered by the profile, reply [INTENT:question] followed by exactly: "
                f'"{self.line("faq_unknown")}"'
            )
        return "\n\n".join(p for p in parts if p)

    # ── Silence rule (§10.1) — called by the watchdog ─────

    async def check_silence(self) -> str:
        """'' | 'reprompted' | 'hung_up'. Only before the first caller utterance."""
        if self.ended or self.last_caller_at is not None or self.sm.is_terminal:
            return ""
        now = self._clock()
        if not self.silence_reprompted:
            if now - self.greeted_at >= SILENCE_REPROMPT_S:
                self.silence_reprompted = True
                self._reprompt_at = now
                await self.speak(self.line("silence_reprompt"))
                return "reprompted"
            return ""
        if now - self._reprompt_at >= SILENCE_HANGUP_S:
            await self.speak(self.line("silence_goodbye"))
            self._metric("silent_hangup")
            self._log_action("silent_hangup", "no caller speech after greeting")
            await self._end("silent_hangup")
            return "hung_up"
        return ""

    # ── Caller turn (before the LLM) ──────────────────────

    async def on_caller_utterance(self, text: str) -> TurnPlan:
        self.turns += 1
        self.last_caller_at = self._clock()
        lowered = str(text or "").strip().lower()
        phase = self.sm.phase

        if self.ended or self.sm.is_terminal:
            return TurnPlan(handled=True)

        # Injection / extraction tripwires (§9) — never argued with
        if any(m in lowered for m in _INJECTION_MARKERS):
            self.injection_attempts += 1
            self._metric("injection_attempt")
            self._log_action("injection_attempt", text)
            if self.injection_attempts >= 2:
                await self.speak(self.line("injection_end"))
                await self._end("injection_blocked")
                return TurnPlan(handled=True)
            await self.speak(str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", self.language) or ""))
            self._sync()
            return TurnPlan(handled=True)
        if any(m in lowered for m in _EXTRACTION_MARKERS):
            self.extraction_attempts += 1
            self._metric("extraction_attempt")
            self._log_action("extraction_attempt", text)
            await self.speak(str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", self.language) or ""))
            return TurnPlan(handled=True)

        if phase == CallPhase.TAKE_MESSAGE or (phase == CallPhase.INBOUND_BOOKING and self.step in _IDENTITY_STEPS):
            return await self._message_step(text)
        if phase in (CallPhase.INBOUND_BOOKING, CallPhase.VERIFY, CallPhase.EXECUTE) and self.step.startswith("b_"):
            return await self._booking_step(text)
        if phase == CallPhase.TRANSFERRING:
            return TurnPlan(handled=True)  # the call is leaving us
        if phase == CallPhase.CONFIRM:
            # "Kann ich sonst noch helfen?" after a booking
            if self._is_goodbye(lowered) or parse_confirmation(lowered) == ConfirmationStatus.REJECTED:
                await self.speak(self.line("message_done") if self.slots.matter else self.line("silence_goodbye"))
                await self._end("caller_done")
                return TurnPlan(handled=True)
            self._to_phase(CallPhase.RECEPTION_INTENT, "more_requests")
            return TurnPlan(system_note=self._intent_note())
        if phase in (CallPhase.RECEPTION_INTENT, CallPhase.FAQ_ANSWER, CallPhase.AFTER_HOURS):
            if self._is_goodbye(lowered):
                await self.speak(self.line("silence_goodbye") if not self.slots.matter else self.line("message_done"))
                await self._end("caller_goodbye")
                return TurnPlan(handled=True)
            if any(m in lowered for m in _HUMAN_MARKERS):
                # §8.4: the intent `human` is never argued with — no LLM needed
                await self._handle_intent(INTENT_HUMAN, "")
                return TurnPlan(handled=True)
            if phase == CallPhase.FAQ_ANSWER:
                self._to_phase(CallPhase.RECEPTION_INTENT, "answered")
            return TurnPlan(system_note=self._intent_note())
        # Anything else (e.g. generic phases) → LLM with the rules
        return TurnPlan(system_note="")

    def _intent_note(self) -> str:
        return str(get_prompt("RECEPTIONIST_INTENT_INSTRUCTION", self.language) or "")

    @staticmethod
    def _is_goodbye(lowered: str) -> bool:
        return any(m in lowered for m in _GOODBYE_MARKERS)

    # ── LLM response (after the turn) ─────────────────────

    async def process_response(self, text: str) -> tuple[str, bool]:
        """Parse/strip the [INTENT:…] token and route. Returns (text to speak,
        suppress_rest) — suppress_rest=True means the session already spoke
        the outcome and the model's remaining text must not be spoken."""
        if self.ended:
            return "", True
        intent, stripped = parse_intent_token(text)
        if intent is None or self.sm.phase != CallPhase.RECEPTION_INTENT:
            return stripped, False
        return await self._handle_intent(intent, stripped)

    async def _handle_intent(self, intent: str, stripped: str) -> tuple[str, bool]:
        self._metric("intent", intent=intent)
        if intent == INTENT_QUESTION:
            self.intent = self.intent or INTENT_QUESTION
            self._to_phase(CallPhase.FAQ_ANSWER, "intent_question")
            unknown_line = self.line("faq_unknown")
            if not stripped.strip() or stripped.strip()[:25] == unknown_line[:25]:
                # Not in the profile → pass it on (§8.1); the session speaks the line
                self._start_message("message")
                self.intent = INTENT_MESSAGE
                self._sync()
                await self.speak(unknown_line)
                return "", True
            # Answered from the profile; back to intent capture on the next turn
            self._sync()
            return stripped, False
        if intent == INTENT_MESSAGE:
            self.intent = INTENT_MESSAGE
            self._start_message("message")
            text = f"{self.line('to_message')} {self.line('ask_name')}"
            await self.speak(text)
            return "", True
        if intent == INTENT_APPOINTMENT:
            self.intent = INTENT_APPOINTMENT
            if not self.profile.booking.enabled:
                self._start_message("message")
                await self.speak(f"{self.line('booking_disabled')} {self.line('ask_name')}")
                return "", True
            self._to_phase(CallPhase.INBOUND_BOOKING, "intent_appointment")
            self.step = "b_timeframe"
            self._sync()
            await self.speak(self.line("ask_timeframe"))
            return "", True
        if intent == INTENT_HUMAN:
            self.intent = INTENT_HUMAN
            if not self.profile.transfer.enabled:
                self._start_message("message")
                await self.speak(f"{self.line('human_disabled')} {self.line('ask_name')}")
                return "", True
            await self._transfer()
            return "", True
        # unknown
        self.unknown_intents += 1
        self._sync()
        if self.unknown_intents >= MAX_UNKNOWN_INTENTS:
            self.intent = INTENT_MESSAGE
            self._start_message("message")
            await self.speak(f"{self.line('to_message')} {self.line('ask_name')}")
            return "", True
        await self.speak(self.line("clarify"))
        return "", True

    # ── TAKE_MESSAGE (§8.2) ───────────────────────────────

    def _start_message(self, then: str) -> None:
        self._identity_then = then
        self.step = "name"
        self.slots.name_attempts = 0
        self.slots.number_attempts = 0
        if self.sm.phase != CallPhase.TAKE_MESSAGE and then == "message":
            self._to_phase(CallPhase.TAKE_MESSAGE, "take_message")
        self._sync()

    async def _message_step(self, text: str) -> TurnPlan:
        step = self.step
        lowered = str(text or "").strip().lower()
        confirm = parse_confirmation(lowered)

        if step == "name":
            name = sl.normalize_name(text)
            self.slots.name_attempts += 1
            if not name:
                await self.speak(self.line("ask_name"))
                return self._handled()
            self.slots.caller_name = name
            if sl.needs_spellback(name, self._confidence()):
                self.step = "name_confirm"
                await self.speak(self.line("spellback", spelled=sl.spell_out(name, self.language)))
                return self._handled()
            return await self._ask_number()

        if step == "name_confirm":
            if confirm == ConfirmationStatus.CONFIRMED:
                self.slots.caller_name_unverified = False
                return await self._ask_number()
            if confirm == ConfirmationStatus.UNCLEAR and not self._yes_no_retry:
                self._yes_no_retry = True
                await self.speak(self.line("yes_no"))
                return self._handled()
            self._yes_no_retry = False
            if self.slots.name_attempts < sl.MAX_SLOT_ATTEMPTS:
                self.step = "name"
                await self.speak(self.line("ask_name"))
                return self._handled()
            self.slots.caller_name_unverified = True  # best effort after 2 attempts
            return await self._ask_number()

        if step == "number":
            digits = sl.extract_digits(text, self.language)
            if len(digits.lstrip("+")) >= 6:
                return await self._take_dictated_number(digits)
            if confirm == ConfirmationStatus.CONFIRMED and self.caller_id:
                self.slots.callback_number = self.caller_id
                self.slots.callback_unverified = False
                return await self._after_identity()
            if confirm == ConfirmationStatus.UNCLEAR and not self._yes_no_retry and self.caller_id:
                self._yes_no_retry = True
                await self.speak(self.line("yes_no"))
                return self._handled()
            self._yes_no_retry = False
            self.step = "number_dictate"
            await self.speak(self.line("ask_number_dictate"))
            return self._handled()

        if step == "number_dictate":
            return await self._take_dictated_number(sl.extract_digits(text, self.language))

        if step == "number_confirm":
            if confirm == ConfirmationStatus.CONFIRMED:
                self.slots.callback_unverified = False
                return await self._after_identity()
            if confirm == ConfirmationStatus.UNCLEAR and not self._yes_no_retry:
                self._yes_no_retry = True
                await self.speak(self.line("yes_no"))
                return self._handled()
            self._yes_no_retry = False
            if self.slots.number_attempts < sl.MAX_SLOT_ATTEMPTS:
                self.step = "number_dictate"
                await self.speak(self.line("ask_number_dictate"))
                return self._handled()
            self.slots.callback_unverified = True
            return await self._after_identity()

        if step == "matter":
            matter, needs_summary = sl.cap_matter(text)
            if not matter:
                await self.speak(self.line("ask_matter"))
                return self._handled()
            self.slots.matter = matter
            if needs_summary:
                self.step = "matter_confirm"
                await self.speak(self.line("matter_summary", summary=matter))
                return self._handled()
            return await self._after_matter()

        if step == "matter_confirm":
            if confirm == ConfirmationStatus.REJECTED and not self.slots.extra.get("matter_retry"):
                self.slots.extra["matter_retry"] = True
                self.step = "matter"
                await self.speak(self.line("ask_matter"))
                return self._handled()
            return await self._after_matter()

        if step == "urgent":
            self.slots.urgent = confirm == ConfirmationStatus.CONFIRMED
            return await self._verify_message()

        if step == "verify_confirm":
            if confirm == ConfirmationStatus.CONFIRMED:
                self._log_action("message_taken", self.slots.to_dict().__repr__())
                self._metric("message_taken", intent=self.intent)
                await self.speak(self.line("message_done"))
                await self._end("message_taken")
                return self._handled()
            if confirm == ConfirmationStatus.UNCLEAR and not self._yes_no_retry:
                self._yes_no_retry = True
                await self.speak(self.line("yes_no"))
                return self._handled()
            self._yes_no_retry = False
            if self.message_retries < 1:
                self.message_retries += 1
                self.slots = sl.MessageSlots()
                self.step = "name"
                await self.speak(f"{self.line('message_retry')} {self.line('ask_name')}")
                return self._handled()
            # second rejection: keep best effort and close
            self.slots.caller_name_unverified = True
            await self.speak(self.line("message_done"))
            await self._end("message_taken_unverified")
            return self._handled()

        # Unknown step → ask the name
        self.step = "name"
        await self.speak(self.line("ask_name"))
        return self._handled()

    def _confidence(self) -> float | None:
        value = self.state.metadata.get("last_stt_confidence")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _handled(self) -> TurnPlan:
        self._sync()
        return TurnPlan(handled=True)

    async def _ask_number(self) -> TurnPlan:
        self.step = "number"
        self._yes_no_retry = False
        if self.caller_id:
            await self.speak(self.line("ask_number", last4=sl.spoken_last4(self.caller_id, self.language)))
        else:
            self.step = "number_dictate"
            await self.speak(self.line("ask_number_dictate"))
        return self._handled()

    async def _take_dictated_number(self, digits: str) -> TurnPlan:
        self.slots.number_attempts += 1
        normalized = sl.normalize_callback_number(digits, self.caller_id)
        if normalized:
            self.slots.callback_number = normalized
            self.step = "number_confirm"
            await self.speak(self.line("number_readback", readback=sl.readback_number(normalized, self.language)))
            return self._handled()
        if self.slots.number_attempts >= sl.MAX_SLOT_ATTEMPTS:
            self.slots.callback_number = digits or self.caller_id
            self.slots.callback_unverified = True
            return await self._after_identity()
        self.step = "number_dictate"
        await self.speak(self.line("ask_number_dictate"))
        return self._handled()

    async def _after_identity(self) -> TurnPlan:
        if self._identity_then == "booking":
            return await self._booking_after_identity()
        self.step = "matter"
        await self.speak(self.line("ask_matter"))
        return self._handled()

    async def _after_matter(self) -> TurnPlan:
        if sl.sounds_urgent(self.slots.matter):
            self.step = "urgent"
            await self.speak(self.line("ask_urgent"))
            return self._handled()
        return await self._verify_message()

    async def _verify_message(self) -> TurnPlan:
        self.step = "verify_confirm"
        self._yes_no_retry = False
        number = (
            sl.readback_number(self.slots.callback_number, self.language)
            if self.slots.callback_number
            else self.line("unknown")
        )
        await self.speak(
            self.line(
                "message_verify",
                name=self.slots.caller_name or self.line("unknown"),
                number=number,
                matter=self.slots.matter or "-",
                urgent=self.line("urgent_suffix") if self.slots.urgent else "",
            )
        )
        return self._handled()

    # ── INBOUND_BOOKING (§8.3) ────────────────────────────

    async def _booking_step(self, text: str) -> TurnPlan:
        step = self.step
        lowered = str(text or "").strip().lower()
        confirm = parse_confirmation(lowered)
        now = self._now_fn()

        if step == "b_timeframe":
            window = bk.resolve_booking_window(lowered, now)
            self.booking.window = window
            busy = await self._freebusy(window[0], window[1])
            if busy is None:
                await self.speak(f"{self.line('booking_failed')} {self.line('ask_name')}")
                self._start_message("message")
                return self._handled()
            self.booking.busy = busy
            self.booking.candidates = bk.compute_profile_candidates(
                busy,
                window[0],
                window[1],
                self.profile,
                duration_minutes=self.profile.booking.event_duration_min,
                buffer_minutes=int(getattr(self.settings, "slot_buffer_min", 15) or 0),
                now=now,
            )
            if not self.booking.candidates:
                self._start_message("message")
                await self.speak(f"{self.line('no_slots')} {self.line('ask_name')}")
                return self._handled()
            return await self._offer_slots()

        if step == "b_choose":
            chosen = bk.parse_slot_choice(lowered, self.booking.candidates, self.language)
            if chosen is None and confirm == ConfirmationStatus.CONFIRMED and len(self.booking.candidates) == 1:
                chosen = self.booking.candidates[0]
            if chosen is None:
                counter = bk.parse_counter_proposal(text, now, self.language)
                duration = timedelta(minutes=self.profile.booking.event_duration_min)
                buffer = timedelta(minutes=int(getattr(self.settings, "slot_buffer_min", 15) or 0))
                if (
                    counter is not None
                    and counter > now
                    and bk.within_hours(counter, duration, self.profile)
                    and bk.is_slot_free(counter, duration, self.booking.busy, buffer)
                ):
                    chosen = counter
            if chosen is None:
                self.booking.declines += 1
                if self.booking.declines >= MAX_BOOKING_DECLINES or not self.booking.candidates:
                    self._start_message("message")
                    await self.speak(f"{self.line('to_message')} {self.line('ask_name')}")
                    return self._handled()
                alternative = spoken_datetime(self.booking.candidates[0], self.language)
                await self.speak(self.line("counter_unavailable", alternative=alternative))
                return self._handled()
            self.booking.chosen = chosen
            self.booking.slot_spoken = spoken_datetime(chosen, self.language)
            # Identity BEFORE the calendar write (§8.3.3) — unless already taken
            # earlier in this call (e.g. after a slot-taken retry).
            self._identity_then = "booking"
            if self.slots.caller_name and self.slots.callback_number:
                return await self._booking_after_identity()
            self.step = "name"
            self.slots.name_attempts = 0
            self.slots.number_attempts = 0
            await self.speak(self.line("ask_name"))
            return self._handled()

        if step == "b_email":
            if any(w in lowered for w in _DECLINE_EMAIL) and "@" not in lowered and " at " not in f" {lowered} ":
                return await self._booking_verify()
            email = sl.extract_spelled_email(text)
            self.booking.email_attempts += 1
            if email:
                self.slots.email = email
                self.step = "b_email_confirm"
                await self.speak(self.line("email_readback", email=sl.spell_email(email, self.language)))
                return self._handled()
            if self.booking.email_attempts >= 2:
                self.slots.email = ""
                return await self._booking_verify()
            await self.speak(self.line("ask_email"))
            return self._handled()

        if step == "b_email_confirm":
            if confirm == ConfirmationStatus.CONFIRMED:
                return await self._booking_verify()
            if self.booking.email_attempts >= 2:
                self.slots.email = ""
                return await self._booking_verify()
            self.step = "b_email"
            await self.speak(self.line("ask_email"))
            return self._handled()

        if step == "b_verify":
            if confirm == ConfirmationStatus.CONFIRMED:
                return await self._write_booking()
            if confirm == ConfirmationStatus.UNCLEAR and not self._yes_no_retry:
                self._yes_no_retry = True
                await self.speak(self.line("yes_no"))
                return self._handled()
            self._yes_no_retry = False
            self.booking.declines += 1
            self._to_phase(CallPhase.INBOUND_BOOKING, "booking_not_confirmed")
            if self.booking.declines >= MAX_BOOKING_DECLINES:
                self._start_message("message")
                await self.speak(f"{self.line('to_message')} {self.line('ask_matter')}")
                self.step = "matter"
                return self._handled()
            return await self._offer_slots()

        self.step = "b_timeframe"
        await self.speak(self.line("ask_timeframe"))
        return self._handled()

    async def _offer_slots(self) -> TurnPlan:
        self.step = "b_choose"
        spoken = [spoken_datetime(c, self.language) for c in self.booking.candidates[: bk.MAX_INBOUND_CANDIDATES]]
        joiner = self.line("and")
        listed = ", ".join(spoken[:-1]) + joiner + spoken[-1] if len(spoken) > 1 else spoken[0]
        await self.speak(self.line("offer_slots", slots=listed))
        return self._handled()

    async def _booking_after_identity(self) -> TurnPlan:
        if self.profile.booking.ask_email:
            self.step = "b_email"
            self.booking.email_attempts = 0
            await self.speak(self.line("ask_email"))
            return self._handled()
        return await self._booking_verify()

    async def _booking_verify(self) -> TurnPlan:
        self.step = "b_verify"
        self._yes_no_retry = False
        # VERIFY before write (§8.3.7) — valid transition from INBOUND_BOOKING
        self._to_phase(CallPhase.INBOUND_BOOKING, "booking_identity_done")
        self._to_phase(CallPhase.VERIFY, "booking_verify")
        await self.speak(
            self.line(
                "booking_verify",
                slot=self.booking.slot_spoken,
                duration=self.profile.booking.event_duration_min,
                name=self.slots.caller_name or self.line("unknown"),
            )
        )
        return self._handled()

    async def _write_booking(self) -> TurnPlan:
        chosen = self.booking.chosen
        assert chosen is not None
        duration = timedelta(minutes=self.profile.booking.event_duration_min)
        # §8.3.6 fresh free/busy re-check at write time
        busy_now = await self._freebusy(chosen - timedelta(minutes=1), chosen + duration + timedelta(minutes=1))
        if busy_now is not None and not bk.is_slot_free(chosen, duration, busy_now):
            self.booking.busy = busy_now
            remaining = [c for c in self.booking.candidates if c != chosen and bk.is_slot_free(c, duration, busy_now)]
            self.booking.candidates = remaining
            self.booking.chosen = None
            self._to_phase(CallPhase.INBOUND_BOOKING, "slot_taken")
            if not remaining:
                self._start_message("message")
                await self.speak(f"{self.line('no_slots')} {self.line('ask_matter')}")
                self.step = "matter"
                return self._handled()
            self.step = "b_choose"
            await self.speak(self.line("slot_taken", alternative=spoken_datetime(remaining[0], self.language)))
            return self._handled()

        title = self.profile.booking.event_title_template.format(caller_name=self.slots.caller_name or "-")
        args: dict[str, Any] = {
            "summary": title,
            "start": chosen.isoformat(),
            "end": (chosen + duration).isoformat(),
            "calendar_id": str(getattr(self.settings, "scheduling_calendar_id", "") or "primary"),
            "description": (
                f"Gebucht via Pincer Rezeption, Anrufer: {self.slots.caller_name or '-'}, "
                f"{self.slots.callback_number or self.caller_id or '-'}, call {self.call_sid}"
            ),
            "timezone": self.profile.business.timezone,
            "idempotency_key": f"pincer-reception-{self.call_sid}",
        }
        if self.slots.email:
            args["attendees"] = self.slots.email
            args["send_updates"] = "all"
        result = await self._run_tool("google__create_event", args)
        if result is None or not result.executed:
            self._to_phase(CallPhase.INBOUND_BOOKING, "booking_failed")
            self._start_message("message")
            self.step = "matter"
            await self.speak(f"{self.line('booking_failed')} {self.line('ask_matter')}")
            return self._handled()
        self.booking.booked = True
        raw = str(getattr(result, "raw", "") or "")
        link = re.search(r"Link:\s*(\S+)", raw)
        event_id = re.search(r"ID:\s*(\S+)", raw)
        self.booking.calendar_link = link.group(1) if link else ""
        self.booking.event_id = event_id.group(1) if event_id else ""
        self._metric("booking", intent=INTENT_APPOINTMENT)
        self._sync()
        if self.pending_faq:
            # Mixed request (§7.2): confirm, then answer the FAQ inline via the LLM
            question, self.pending_faq = self.pending_faq, ""
            await self.speak(self.line("booking_done", slot=self.booking.slot_spoken).rsplit(".", 2)[0] + ".")
            self._to_phase(CallPhase.CONFIRM, "booked")
            self._to_phase(CallPhase.RECEPTION_INTENT, "pending_faq")
            self._sync()
            return TurnPlan(handled=False, system_note=self._intent_note(), override_text=question)
        await self.speak(self.line("booking_done", slot=self.booking.slot_spoken))
        self._to_phase(CallPhase.CONFIRM, "booked")
        await self._end("booked")
        return self._handled()

    # ── Tools (through the Sprint 11 gate) ────────────────

    async def _run_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self.gate is None or self.tools is None:
            return None
        tools = self.tools

        async def _exec() -> tuple[str, bool]:
            try:
                out = await tools.execute(name, dict(args))
                return str(out), str(out).strip().lower().startswith("error")
            except Exception as e:  # noqa: BLE001 - tool contract: never raise into the call
                return f"Error: {e}", True

        captured: dict[str, str] = {}

        async def _exec_capture() -> tuple[str, bool]:
            content, is_error = await _exec()
            captured["raw"] = content
            return content, is_error

        result = await self.gate.run(name, args, _exec_capture)
        with contextlib.suppress(Exception):
            result.raw = captured.get("raw", "")  # type: ignore[attr-defined]
        return result

    async def _freebusy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
        """Busy windows only (never event contents). None = unknown (never 'free')."""
        from pincer.voice.scheduling import parse_freebusy_output

        result = await self._run_tool(
            "google__check_freebusy",
            {
                "emails": str(getattr(self.settings, "scheduling_calendar_id", "") or "primary"),
                "time_min": start.isoformat(),
                "time_max": end.isoformat(),
            },
        )
        if result is None or not result.executed:
            return None
        return parse_freebusy_output(str(getattr(result, "raw", "") or ""))

    # ── TRANSFERRING (§8.4) ───────────────────────────────

    async def _transfer(self) -> None:
        self.transfer_attempted = True
        self._to_phase(CallPhase.TRANSFERRING, "intent_human")
        self._metric("transfer")
        self._sync()
        action_url = ""
        base = str(getattr(self.settings, "voice_webhook_base_url", "") or "").strip().rstrip("/")
        if base.startswith("http"):
            action_url = f"{base}/api/apps/twilio/transfer-result"
        announce = self.profile.transfer.announce or ""
        try:
            if self.engine is None:
                raise RuntimeError("no engine")
            await self.engine.transfer_call(
                self.call_sid,
                self.profile.transfer.target,
                timeout_s=TRANSFER_DIAL_TIMEOUT_S,
                action_url=action_url,
                announce=announce,
                language=self.language,
            )
            self._log_action("transfer", self.profile.transfer.target)
        except Exception:
            logger.exception("Receptionist transfer failed [%s]", self.call_sid)
            await self.on_transfer_failed()

    async def on_transfer_failed(self) -> str:
        """Dial failed/busy/no-answer (webhook or immediate): apology → TAKE_MESSAGE.
        Returns the text to speak (also used as the re-connect greeting)."""
        self.transfer_failed = True
        self._metric("transfer_failed")
        self._to_phase(CallPhase.TAKE_MESSAGE, "transfer_failed")
        self._start_message("message")
        text = f"{self.line('transfer_failed')} {self.line('ask_name')}"
        self._sync()
        return text

    async def on_call_end(self) -> None:
        self._sync()


_IDENTITY_STEPS = {"name", "name_confirm", "number", "number_dictate", "number_confirm"}


__all__ = [
    "MAX_BOOKING_DECLINES",
    "MAX_UNKNOWN_INTENTS",
    "SILENCE_HANGUP_S",
    "SILENCE_REPROMPT_S",
    "TRANSFER_DIAL_TIMEOUT_S",
    "BookingState",
    "ReceptionSession",
    "TurnPlan",
    "opening_text",
]
