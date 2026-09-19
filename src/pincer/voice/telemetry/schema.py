"""
The telephony telemetry schema: event names, span names, and — the part that
actually matters — the *definition* of every latency metric.

A latency number without its boundaries is a rumour. Every metric here declares
its start event, its end event, what measured it, which engines can produce it,
and what it cannot tell you. The API serves this table verbatim
(`GET /api/telephony/metrics`) and the dashboard renders the definition next to
the number, so the meaning travels with the value instead of living in a wiki.

Availability is per engine on purpose (see docs/telephony-pipeline.md §2):
ConversationRelay hands us text, Media Streams hands us audio, and pretending
they are the same pipeline is how "we have TTS latency" turns into a chart of
zeros.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventName(StrEnum):
    """Point-in-time facts. Cheap, always recorded for a sampled call."""

    # ── lifecycle ──
    INBOUND_WEBHOOK = "call.inbound_webhook"
    CALL_REGISTERED = "call.registered"
    DIAL_REQUESTED = "call.dial_requested"
    DIAL_ACCEPTED = "call.dial_accepted"
    DIAL_REJECTED = "call.dial_rejected"
    PROVIDER_STATUS = "call.provider_status"
    AMD_VERDICT = "call.amd_verdict"
    MEDIA_STREAM_OPEN = "call.media_stream_open"
    CALL_ANSWERED = "call.answered"
    FIRST_INBOUND_AUDIO = "call.first_inbound_audio"
    MEDIA_STREAM_CLOSED = "call.media_stream_closed"
    CALL_ENDED = "call.ended"
    CALL_DECLINED = "call.declined"
    #: A call LIFECYCLE state transition. Separate from the pipeline spans
    #: on purpose: the call has states, the streaming pipeline does not.
    CALL_PHASE = "call.phase"

    # ── conversation ──
    STT_SPEECH_START = "stt.speech_start"
    STT_SPEECH_END = "stt.speech_end"
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    ENDPOINT_DECISION = "stt.endpoint_decision"
    TURN_START = "turn.start"
    TURN_QUEUED = "turn.queued"
    TURN_HANDLED_LOCALLY = "turn.handled_locally"
    AGENT_PREP_DONE = "agent.prep_done"
    LLM_REQUEST = "llm.request"
    LLM_FIRST_TOKEN = "llm.first_token"
    LLM_DONE = "llm.done"
    SENTENCE_READY = "tts.sentence_ready"
    TTS_REQUEST = "tts.request"
    TTS_FIRST_AUDIO = "tts.first_audio"
    TTS_DONE = "tts.done"
    AUDIO_QUEUED = "audio.queued"
    AUDIO_DISPATCHED = "audio.dispatched"
    PLAYBACK_MARK = "audio.playback_mark"
    TOOL_START = "tool.start"
    TOOL_END = "tool.end"

    # ── disruption ──
    BARGE_IN_DETECTED = "bargein.detected"
    TURN_CANCELLED = "turn.cancelled"
    BUFFER_CLEARED = "audio.buffer_cleared"
    AUDIO_GAP = "audio.gap"
    ERROR = "error"
    TIMEOUT = "timeout"
    RECONNECT = "reconnect"
    TURN_END = "turn.end"


class SpanName(StrEnum):
    """Intervals. These overlap on purpose — the pipeline is streaming."""

    CALL = "call"
    CALL_SETUP = "call.setup"
    MEDIA = "call.media"
    TURN = "turn"
    AGENT_PREP = "agent.prep"
    LLM = "llm.generation"
    TOOL = "tool.execution"
    TTS = "tts.synthesis"
    AUDIO_OUT = "audio.outbound"
    STT_UTTERANCE = "stt.utterance"
    APPROVAL = "tool.approval"


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DENIED = "denied"
    DEFERRED = "deferred"


class MeasurementSource(StrEnum):
    """Where a number came from. Rendered as a badge next to every latency."""

    #: Both boundaries are monotonic readings taken inside this process.
    SERVER_MEASURED = "server_measured"
    #: The provider told us; we did not observe it.
    PROVIDER_REPORTED = "provider_reported"
    #: Derived from something adjacent (e.g. text dispatch standing in for audio).
    ESTIMATED = "estimated"
    #: Structurally unobservable in this deployment.
    UNAVAILABLE = "unavailable"


# Engines, spelled exactly as `VoiceEngine.engine_name` returns them.
CONVERSATION_RELAY = "conversation_relay"
MEDIA_STREAMS = "media_streams"
ALL_ENGINES = (CONVERSATION_RELAY, MEDIA_STREAMS)


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    key: str
    label: str
    start_event: str
    end_event: str
    source: MeasurementSource
    unit: str = "ms"
    available_on: tuple[str, ...] = ALL_ENGINES
    limitations: str = ""
    unavailable_reason: str = ""

    def available(self, engine: str) -> bool:
        return engine in self.available_on

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "start_event": self.start_event,
            "end_event": self.end_event,
            "source": str(self.source),
            "unit": self.unit,
            "available_on": list(self.available_on),
            "limitations": self.limitations,
            "unavailable_reason": self.unavailable_reason,
        }


METRICS: dict[str, MetricDefinition] = {
    d.key: d
    for d in (
        MetricDefinition(
            key="call_setup_ms",
            label="Call setup",
            start_event=EventName.DIAL_REQUESTED,
            end_event=EventName.CALL_ANSWERED,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "Outbound only. Includes carrier ring time, which is the callee's behaviour, "
                "not ours — a slow p95 here usually means people took longer to pick up."
            ),
        ),
        MetricDefinition(
            key="media_establish_ms",
            label="Media establishment",
            start_event=EventName.CALL_ANSWERED,
            end_event=EventName.MEDIA_STREAM_OPEN,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "The call's first media socket only; a mid-call reconnect is a timeline event, "
                "not a second establishment. On both engines the WebSocket open IS the answer "
                "signal, so this is zero on almost every call — it is non-zero only when the "
                "answer reached us over a separate webhook before the socket connected."
            ),
        ),
        MetricDefinition(
            key="first_inbound_audio_ms",
            label="First inbound audio",
            start_event=EventName.MEDIA_STREAM_OPEN,
            end_event=EventName.FIRST_INBOUND_AUDIO,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            unavailable_reason="ConversationRelay delivers transcribed text; no audio frame ever reaches us.",
        ),
        MetricDefinition(
            key="endpointing_ms",
            label="Endpointing delay",
            start_event=EventName.STT_SPEECH_END,
            end_event=EventName.ENDPOINT_DECISION,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            limitations=(
                "Speech end is Deepgram's word-level end time for the last word of the "
                "utterance, projected onto our clock — it is the provider's opinion of when "
                "the caller stopped, not an independent measurement."
            ),
            unavailable_reason="Twilio performs VAD and endpointing inside ConversationRelay and reports neither.",
        ),
        MetricDefinition(
            key="stt_first_partial_ms",
            label="STT first partial",
            start_event=EventName.STT_SPEECH_START,
            end_event=EventName.STT_PARTIAL,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            unavailable_reason="ConversationRelay does not forward interim results.",
        ),
        MetricDefinition(
            key="stt_final_ms",
            label="STT finalisation",
            start_event=EventName.STT_SPEECH_END,
            end_event=EventName.STT_FINAL,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            limitations="Contains the endpointing wait; it is not pure recognition time.",
            unavailable_reason="ConversationRelay reports only the finished transcript, with no speech-end reference.",
        ),
        MetricDefinition(
            key="agent_queue_ms",
            label="Agent queueing",
            start_event=EventName.STT_FINAL,
            end_event=EventName.TURN_START,
            source=MeasurementSource.SERVER_MEASURED,
            limitations="Non-zero mainly when a previous turn is still being cancelled (barge-in).",
        ),
        MetricDefinition(
            key="agent_prep_ms",
            label="Agent preparation",
            start_event=EventName.TURN_START,
            end_event=EventName.AGENT_PREP_DONE,
            source=MeasurementSource.SERVER_MEASURED,
            limitations="Session load, memory search and prompt assembly. Excludes the provider request.",
        ),
        MetricDefinition(
            key="llm_ttft_ms",
            label="LLM time to first token",
            start_event=EventName.LLM_REQUEST,
            end_event=EventName.LLM_FIRST_TOKEN,
            source=MeasurementSource.SERVER_MEASURED,
            limitations="Network time to the provider is included; the providers do not report a server-side split.",
        ),
        MetricDefinition(
            key="llm_total_ms",
            label="LLM generation",
            start_event=EventName.LLM_REQUEST,
            end_event=EventName.LLM_DONE,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "Covers every iteration of the tool loop for this turn, so it overlaps tool "
                "spans and must never be added to them."
            ),
        ),
        MetricDefinition(
            # Must match the stage COLUMN, not the span name: METRICS is keyed
            # by `key` and every consumer looks a stage up by its column —
            # `queries.STAGE_COLUMNS`, `store.TURN_FIELDS`, the dashboard's
            # `byKey.get(row.key)`. Registered as "tool_ms" this silently
            # resolved to None and dropped the limitations note below, which is
            # the one stage where it changes how the number should be read.
            key="tool_total_ms",
            label="Tool execution",
            start_event=EventName.TOOL_START,
            end_event=EventName.TOOL_END,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "Per tool AND per attempt. Tools may run in parallel within one LLM iteration; "
                "a verbal or dashboard approval hold is recorded as its own span, not as tool time."
            ),
        ),
        MetricDefinition(
            key="tts_first_audio_ms",
            label="TTS time to first audio",
            start_event=EventName.TTS_REQUEST,
            end_event=EventName.TTS_FIRST_AUDIO,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            unavailable_reason="ConversationRelay synthesises inside Twilio; we hand it text and never see audio.",
        ),
        MetricDefinition(
            key="tts_total_ms",
            label="TTS synthesis",
            start_event=EventName.TTS_REQUEST,
            end_event=EventName.TTS_DONE,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            unavailable_reason="See TTS time to first audio.",
        ),
        MetricDefinition(
            key="audio_queue_ms",
            label="Outbound audio queue residence",
            start_event=EventName.AUDIO_QUEUED,
            end_event=EventName.AUDIO_DISPATCHED,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "Time a chunk waited in OUR process before being written to the provider socket. "
                "It says nothing about the provider's own playout buffer, which is where the "
                "audio actually sits."
            ),
        ),
        MetricDefinition(
            key="response_latency_ms",
            label="Response latency (audio sent)",
            start_event=EventName.STT_SPEECH_END,
            end_event=EventName.AUDIO_DISPATCHED,
            source=MeasurementSource.SERVER_MEASURED,
            limitations=(
                "Ends when the first response audio (Media Streams) or first text token "
                "(ConversationRelay) was written to the provider. This is SENT, not heard. "
                "On ConversationRelay the start boundary falls back to transcript arrival, "
                "which makes the number smaller than reality by the endpointing wait."
            ),
        ),
        MetricDefinition(
            key="playback_response_latency_ms",
            label="Playback response latency",
            start_event=EventName.STT_SPEECH_END,
            end_event=EventName.PLAYBACK_MARK,
            source=MeasurementSource.PROVIDER_REPORTED,
            available_on=(),
            unavailable_reason=(
                "Requires provider playback signals. ConversationRelay has none; Media Streams "
                "supports `mark` echoes but this deployment does not emit marks, so no playback "
                "signal exists to measure against."
            ),
        ),
        MetricDefinition(
            key="caller_perceived_latency_ms",
            label="Caller-perceived latency",
            start_event="caller stops speaking (acoustic)",
            end_event="caller hears first audio (acoustic)",
            source=MeasurementSource.UNAVAILABLE,
            available_on=(),
            unavailable_reason=(
                "Only measurable with an end-to-end audio probe on the PSTN leg. Nothing in this "
                "process observes the caller's ear, and PSTN/carrier delay is outside our view. "
                "Response latency is a LOWER BOUND for it, never a substitute."
            ),
        ),
        MetricDefinition(
            key="interruption_latency_ms",
            label="Interruption latency",
            start_event=EventName.BARGE_IN_DETECTED,
            end_event=EventName.BUFFER_CLEARED,
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            limitations=(
                "Ends when the buffer-clear was written to the provider socket. Twilio does not "
                "acknowledge a `clear`, so the caller may keep hearing already-played-out audio "
                "after this instant."
            ),
            unavailable_reason=(
                "ConversationRelay handles barge-in internally and only notifies us afterwards; "
                "there is no cancellation we perform and therefore none to time."
            ),
        ),
        MetricDefinition(
            key="transport_rtt_ms",
            label="Transport RTT / jitter / loss",
            start_event="-",
            end_event="-",
            source=MeasurementSource.UNAVAILABLE,
            available_on=(),
            unavailable_reason=(
                "Twilio terminates the RTP leg. Our transport is a WebSocket carrying already-"
                "jitter-buffered frames, so no RTP statistics exist on our side."
            ),
        ),
        MetricDefinition(
            key="audio_gap_ms",
            label="Inbound audio gap",
            start_event="previous media frame",
            end_event="next media frame",
            source=MeasurementSource.SERVER_MEASURED,
            available_on=(MEDIA_STREAMS,),
            limitations=(
                "Gaps in frame ARRIVAL at our socket. A gap can be network loss, provider "
                "buffering, or our own event loop being blocked — it does not attribute blame."
            ),
            unavailable_reason="No audio frames on ConversationRelay.",
        ),
    )
}


#: The ordered dependency chain used to attribute the critical path to first
#: response audio. Later stages win when spans overlap: while TTS is producing
#: the first chunk, we are waiting on TTS, even though the LLM is still writing.
CRITICAL_PATH_ORDER: tuple[str, ...] = (
    SpanName.STT_UTTERANCE,
    SpanName.TURN,
    SpanName.AGENT_PREP,
    SpanName.LLM,
    SpanName.TOOL,
    SpanName.APPROVAL,
    SpanName.TTS,
    SpanName.AUDIO_OUT,
)


def metrics_for_engine(engine: str) -> list[MetricDefinition]:
    return [m for m in METRICS.values() if m.available(engine)]


def unavailable_for_engine(engine: str) -> list[MetricDefinition]:
    return [m for m in METRICS.values() if not m.available(engine)]
