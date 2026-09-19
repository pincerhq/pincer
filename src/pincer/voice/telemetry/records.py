"""Wire format for telephony telemetry: one event row, one span row.

Both are plain dataclasses with a dict round-trip, because they cross three
boundaries: the in-process queue, SQLite, and the dashboard JSON. Keeping them
dumb is what lets the recorder drop them without side effects when the queue
is full.

Content policy (enforced here, not by convention): ``attributes`` is for
technical facts. Raw audio, transcripts, credentials and tool payloads are
excluded by :func:`safe_attributes`, which is the only way attributes are
built. Phone numbers are masked on the way in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Attribute keys that may never appear in technical telemetry, whatever a
#: caller passes. Transcript and recording access lives behind the existing
#: voice permissions and retention rules, not here.
_FORBIDDEN_KEYS = frozenset(
    {
        "text",
        "transcript",
        "utterance",
        "prompt",
        "response",
        "content",
        "audio",
        "payload",
        "args",
        "arguments",
        "result",
        "output",
        "tool_input",
        "tool_output",
        "api_key",
        "token",
        "authorization",
        "secret",
        "password",
    }
)

#: Keys whose value is a phone number and must be masked rather than dropped —
#: "which number" is a real diagnostic ("all failures are on the +49 30 trunk").
_PHONE_KEYS = frozenset({"from", "to", "caller", "callee", "number", "target", "from_number", "to_number"})

_MAX_ATTR_LEN = 200
_MAX_ATTRS = 32


def _mask_number(value: str) -> str:
    from pincer.voice.pii_guard import mask_phone_number

    return mask_phone_number(value)


def safe_attributes(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Filter, mask and bound an attribute dict.

    Unknown keys are kept (the taxonomy grows), but anything on the forbidden
    list is dropped outright and every string is truncated. A telemetry row is
    a technical artefact; if a value is long enough to need truncating it was
    almost certainly content rather than a fact.
    """
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_ATTRS:
            break
        lowered = str(key).lower()
        if lowered in _FORBIDDEN_KEYS:
            continue
        if value is None:
            continue
        if lowered in _PHONE_KEYS and isinstance(value, str):
            out[key] = _mask_number(value)
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:_MAX_ATTR_LEN]
        else:
            out[key] = str(value)[:_MAX_ATTR_LEN]
    return out


def deterministic_event_id(call_id: str, name: str, discriminator: str) -> str:
    """Stable id for an event a provider may deliver more than once.

    Twilio retries status callbacks; the same status for the same call must
    collapse to one row rather than inflating the timeline. Internal events use
    a random id instead — two identical internal events are two real events.
    """
    digest = hashlib.sha1(f"{call_id}|{name}|{discriminator}".encode(), usedforsecurity=False)
    return digest.hexdigest()[:32]


@dataclass(slots=True)
class TelemetryEvent:
    event_id: str
    call_id: str
    name: str
    ts_utc: str
    mono_ns: int
    seq: int = 0
    trace_id: str = ""
    span_id: str = ""
    turn_id: str = ""
    provider_call_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.event_id,
            self.call_id,
            self.provider_call_id,
            self.trace_id,
            self.span_id,
            self.turn_id,
            self.name,
            self.ts_utc,
            self.mono_ns,
            self.seq,
            json.dumps(self.attributes, ensure_ascii=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "call_id": self.call_id,
            "provider_call_id": self.provider_call_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "turn_id": self.turn_id,
            "name": self.name,
            "ts_utc": self.ts_utc,
            "seq": self.seq,
            "attributes": self.attributes,
        }


@dataclass(slots=True)
class TelemetrySpan:
    span_id: str
    call_id: str
    name: str
    start_utc: str
    start_mono_ns: int
    end_mono_ns: int | None = None
    end_utc: str = ""
    duration_ms: float | None = None
    parent_span_id: str = ""
    trace_id: str = ""
    turn_id: str = ""
    status: str = "ok"
    attempt: int = 1
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.span_id,
            self.call_id,
            self.trace_id,
            self.parent_span_id,
            self.turn_id,
            self.name,
            self.start_utc,
            self.end_utc,
            self.start_mono_ns,
            self.end_mono_ns,
            self.duration_ms,
            self.status,
            self.attempt,
            json.dumps(self.attributes, ensure_ascii=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "call_id": self.call_id,
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "name": self.name,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attempt": self.attempt,
            "attributes": self.attributes,
            # Offsets are filled in by the reader relative to the turn or call
            # origin; the raw monotonic values are process-local and useless
            # to a browser.
            "start_offset_ms": None,
            "end_offset_ms": None,
        }
