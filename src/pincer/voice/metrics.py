"""
Voice latency metrics — per-call timing instrumentation (Sprint 1).

Measures the three numbers that define whether a call feels human:
- time-to-first-agent-word (call start -> first speech sent)
- per-turn round trip (caller utterance received -> agent speech started)
- barge-in reaction time (speech onset -> TTS stopped; <500ms target)

Purely in-memory and cheap; used both on live calls (DEBUG logs / engine
comparison, T1.1) and by the reliability harness (T1.6).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Above this time-to-first-audio-chunk per utterance, TTS latency is logged
# as a WARNING (Sprint 4 latency guard).
TTS_FIRST_CHUNK_WARN_MS = 800.0


@dataclass
class CallMetrics:
    """Timing data for a single call."""

    call_sid: str
    engine: str = ""
    started_at: float = field(default_factory=time.monotonic)
    first_agent_word_s: float | None = None
    turn_latencies_s: list[float] = field(default_factory=list)
    barge_in_reactions_ms: list[float] = field(default_factory=list)
    tts_first_chunk_ms: list[float] = field(default_factory=list)
    tts_characters: int = 0
    _pending_turn_started: float | None = None

    def mark_caller_utterance(self) -> None:
        """A caller utterance arrived — the turn clock starts."""
        self._pending_turn_started = time.monotonic()

    def mark_agent_speech_start(self) -> None:
        """The agent started speaking — closes the pending turn, records TTFW once."""
        now = time.monotonic()
        if self.first_agent_word_s is None:
            self.first_agent_word_s = now - self.started_at
        if self._pending_turn_started is not None:
            self.turn_latencies_s.append(now - self._pending_turn_started)
            self._pending_turn_started = None

    def record_barge_in_reaction(self, reaction_ms: float) -> None:
        self.barge_in_reactions_ms.append(reaction_ms)

    def record_tts_first_chunk(self, latency_ms: float) -> None:
        """Time-to-first-audio-chunk for one utterance (Sprint 4 latency guard)."""
        self.tts_first_chunk_ms.append(latency_ms)
        if latency_ms > TTS_FIRST_CHUNK_WARN_MS:
            logger.warning(
                "TTS first audio chunk took %.0fms (>%.0fms) [%s]",
                latency_ms,
                TTS_FIRST_CHUNK_WARN_MS,
                self.call_sid,
            )

    def record_tts_characters(self, count: int) -> None:
        """Synthesized character count (ElevenLabs bills per character)."""
        self.tts_characters += count

    @property
    def mean_turn_latency_s(self) -> float | None:
        if not self.turn_latencies_s:
            return None
        return sum(self.turn_latencies_s) / len(self.turn_latencies_s)

    def summary(self) -> dict[str, float | int | str | None]:
        mean_barge = (
            sum(self.barge_in_reactions_ms) / len(self.barge_in_reactions_ms) if self.barge_in_reactions_ms else None
        )
        mean_tts = sum(self.tts_first_chunk_ms) / len(self.tts_first_chunk_ms) if self.tts_first_chunk_ms else None
        return {
            "call_sid": self.call_sid,
            "engine": self.engine,
            "time_to_first_agent_word_s": round(self.first_agent_word_s, 3) if self.first_agent_word_s else None,
            "turns": len(self.turn_latencies_s),
            "mean_turn_latency_s": round(self.mean_turn_latency_s, 3) if self.mean_turn_latency_s else None,
            "max_turn_latency_s": round(max(self.turn_latencies_s), 3) if self.turn_latencies_s else None,
            "mean_barge_in_reaction_ms": round(mean_barge, 1) if mean_barge is not None else None,
            "mean_tts_first_chunk_ms": round(mean_tts, 1) if mean_tts is not None else None,
            "tts_characters": self.tts_characters,
        }


class VoiceMetricsRegistry:
    """Holds CallMetrics for active and recently ended calls."""

    MAX_RETAINED = 100

    def __init__(self) -> None:
        self._calls: dict[str, CallMetrics] = {}

    def start_call(self, call_sid: str, engine: str = "") -> CallMetrics:
        metrics = CallMetrics(call_sid=call_sid, engine=engine)
        self._calls[call_sid] = metrics
        if len(self._calls) > self.MAX_RETAINED:
            oldest = next(iter(self._calls))
            self._calls.pop(oldest, None)
        return metrics

    def get(self, call_sid: str) -> CallMetrics | None:
        return self._calls.get(call_sid)

    def get_or_start(self, call_sid: str, engine: str = "") -> CallMetrics:
        return self._calls.get(call_sid) or self.start_call(call_sid, engine)

    def finish_call(self, call_sid: str) -> dict[str, float | int | str | None] | None:
        """Log and return the summary for an ended call (metrics stay retained)."""
        metrics = self._calls.get(call_sid)
        if not metrics:
            return None
        summary = metrics.summary()
        logger.info("Call latency [%s]: %s", call_sid, summary)
        return summary

    def all_summaries(self) -> list[dict[str, float | int | str | None]]:
        return [m.summary() for m in self._calls.values()]


# Process-wide registry; VoiceChannel and the harness share it per instance instead
# when isolation matters.
metrics_registry = VoiceMetricsRegistry()
