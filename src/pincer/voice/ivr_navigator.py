"""
IVR navigation engine — navigates automated phone menus on behalf of users.

Listens to IVR prompts via STT, uses LLM to determine the correct option,
sends DTMF tones, and detects hold music / human pickup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

HOLD_MUSIC_SILENCE_THRESHOLD_MS = 3000
HUMAN_SPEECH_MIN_WORDS = 5


@dataclass
class IVRMenuOption:
    digit: str
    description: str


@dataclass
class IVRState:
    """Tracks IVR navigation progress for a call."""

    call_sid: str
    goal: str
    menu_history: list[str] = field(default_factory=list)
    dtmf_sent: list[str] = field(default_factory=list)
    hold_started_at: float | None = None
    human_detected: bool = False
    max_hold_seconds: int = 300


class IVRNavigator:
    """Navigates IVR systems during outbound calls."""

    def __init__(self, engine: VoiceEngine, max_hold_seconds: int = 300) -> None:
        self._engine = engine
        self._max_hold_seconds = max_hold_seconds
        self._active_sessions: dict[str, IVRState] = {}

    def start_navigation(self, call_sid: str, goal: str) -> IVRState:
        state = IVRState(
            call_sid=call_sid,
            goal=goal,
            max_hold_seconds=self._max_hold_seconds,
        )
        self._active_sessions[call_sid] = state
        logger.info("IVR navigation started [%s]: %s", call_sid, goal)
        return state

    def stop_navigation(self, call_sid: str) -> None:
        self._active_sessions.pop(call_sid, None)

    async def process_ivr_prompt(self, call_sid: str, transcript: str) -> str | None:
        """Analyze an IVR prompt and determine the action to take.

        Returns the DTMF digit(s) to send, or None if waiting for more input.
        """
        state = self._active_sessions.get(call_sid)
        if not state:
            return None

        state.menu_history.append(transcript)

        if _detect_human_speech(transcript):
            state.human_detected = True
            logger.info("Human detected on call %s", call_sid)
            return None

        options = _parse_menu_options(transcript)
        if not options:
            return None

        best = _select_best_option(options, state.goal)
        if best:
            state.dtmf_sent.append(best.digit)
            logger.info(
                "IVR selection [%s]: press %s for '%s'",
                call_sid, best.digit, best.description,
            )
            await self._engine.send_dtmf(call_sid, best.digit)
            return best.digit

        return None

    def check_hold_status(self, call_sid: str) -> dict[str, Any]:
        """Check if the call is on hold and for how long."""
        state = self._active_sessions.get(call_sid)
        if not state:
            return {"on_hold": False}

        if state.hold_started_at is None:
            return {"on_hold": False}

        hold_seconds = int(time.monotonic() - state.hold_started_at)
        exceeded = hold_seconds > state.max_hold_seconds

        return {
            "on_hold": True,
            "hold_seconds": hold_seconds,
            "max_exceeded": exceeded,
            "human_detected": state.human_detected,
        }

    def mark_on_hold(self, call_sid: str) -> None:
        state = self._active_sessions.get(call_sid)
        if state and state.hold_started_at is None:
            state.hold_started_at = time.monotonic()
            logger.info("Call %s now on hold", call_sid)

    def mark_off_hold(self, call_sid: str) -> None:
        state = self._active_sessions.get(call_sid)
        if state:
            state.hold_started_at = None


def _parse_menu_options(transcript: str) -> list[IVRMenuOption]:
    """Extract menu options from an IVR transcript."""
    options: list[IVRMenuOption] = []

    patterns = [
        re.compile(r"(?:press|dial|hit)\s+(\d+)\s+(?:for|to)\s+(.+?)(?:\.|,|$)", re.I),
        re.compile(r"(?:for|to)\s+(.+?),?\s+(?:press|dial|hit)\s+(\d+)", re.I),
        re.compile(r"(\d+)\s+(?:for|to|is)\s+(.+?)(?:\.|,|$)", re.I),
    ]

    for pattern in patterns:
        for match in pattern.finditer(transcript):
            groups = match.groups()
            if len(groups) == 2:
                if groups[0].isdigit():
                    options.append(IVRMenuOption(digit=groups[0], description=groups[1].strip()))
                elif groups[1].isdigit():
                    options.append(IVRMenuOption(digit=groups[1], description=groups[0].strip()))

    seen = set()
    unique = []
    for opt in options:
        if opt.digit not in seen:
            seen.add(opt.digit)
            unique.append(opt)
    return unique


def _select_best_option(options: list[IVRMenuOption], goal: str) -> IVRMenuOption | None:
    """Select the IVR option that best matches the call goal."""
    goal_lower = goal.lower()

    goal_keywords = set(goal_lower.split())

    best_option: IVRMenuOption | None = None
    best_score = 0

    for option in options:
        desc_lower = option.description.lower()
        score = sum(1 for kw in goal_keywords if kw in desc_lower)

        appointment_words = {"appointment", "schedule", "reschedule", "booking"}
        if goal_keywords & appointment_words and any(w in desc_lower for w in appointment_words):
            score += 3

        operator_words = {"operator", "representative", "agent", "person", "speak"}
        if any(w in desc_lower for w in operator_words):
            score += 1

        if score > best_score:
            best_score = score
            best_option = option

    if best_option and best_score > 0:
        return best_option

    for option in options:
        desc = option.description.lower()
        if any(w in desc for w in ("operator", "representative", "agent", "other")):
            return option

    return None


def _detect_human_speech(transcript: str) -> bool:
    """Detect if a human (vs. IVR system) is speaking."""
    words = transcript.split()
    if len(words) < HUMAN_SPEECH_MIN_WORDS:
        return False

    ivr_indicators = [
        "press", "dial", "menu", "option", "selection",
        "please hold", "your call is important",
        "estimated wait time", "remain on the line",
    ]
    transcript_lower = transcript.lower()
    ivr_matches = sum(1 for ind in ivr_indicators if ind in transcript_lower)

    human_indicators = [
        "hello", "hi", "how can i help", "speaking",
        "this is", "good morning", "good afternoon",
    ]
    human_matches = sum(1 for ind in human_indicators if ind in transcript_lower)

    return human_matches > ivr_matches
