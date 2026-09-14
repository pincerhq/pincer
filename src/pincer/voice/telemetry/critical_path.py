"""
Critical path to first response audio.

The turn pipeline streams and overlaps: the LLM is still writing while the
first sentence is already being synthesised, and tools run inside the LLM span.
So the naive presentation — a stacked bar of stage durations — is wrong twice
over. It double counts (the parts add up to more than the turn) and it lies
about the bottleneck (a 3 s LLM span that overlapped 2.5 s of TTS did not cost
3 s of response latency).

This module answers the only question that matters for a slow turn: *between
the caller falling silent and the first response audio going out, what were we
waiting on at each instant?*

Method
------
Take the window ``[turn_origin, first_audio]``. Cut it at every span boundary
inside it. In each resulting slice, exactly one stage is "the thing we are
waiting on": the **deepest active stage** in the pipeline's dependency order
(``schema.CRITICAL_PATH_ORDER``). While TTS is producing the first chunk we are
waiting on TTS, even though the LLM span is still open. Slices with no active
span are ``unattributed`` — usually scheduler latency, and calling it that is
better than silently folding it into whichever stage happens to be adjacent.

The result therefore **partitions** the window: the segment durations sum to
the response latency exactly. That is what makes it safe to render as a bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pincer.voice.telemetry.schema import CRITICAL_PATH_ORDER

if TYPE_CHECKING:
    from collections.abc import Sequence

UNATTRIBUTED = "unattributed"

_RANK = {name: index for index, name in enumerate(CRITICAL_PATH_ORDER)}
#: Spans not in the dependency order still take part, ranked last-but-one so a
#: future stage shows up instead of vanishing, but never outranks audio output.
_UNKNOWN_RANK = len(CRITICAL_PATH_ORDER) - 1


@dataclass(frozen=True, slots=True)
class PathSpan:
    """The minimum a span needs to take part in the attribution."""

    name: str
    start_ns: int
    end_ns: int | None
    span_id: str = ""
    status: str = "ok"
    attempt: int = 1
    label: str = ""


@dataclass(frozen=True, slots=True)
class PathSegment:
    stage: str
    start_offset_ms: float
    duration_ms: float
    span_id: str = ""
    label: str = ""
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "start_offset_ms": round(self.start_offset_ms, 2),
            "duration_ms": round(self.duration_ms, 2),
            "span_id": self.span_id,
            "label": self.label,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CriticalPath:
    total_ms: float
    segments: tuple[PathSegment, ...]
    bottleneck_stage: str
    bottleneck_ms: float
    unattributed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round(self.total_ms, 2),
            "segments": [s.to_dict() for s in self.segments],
            "bottleneck_stage": self.bottleneck_stage,
            "bottleneck_ms": round(self.bottleneck_ms, 2),
            "unattributed_ms": round(self.unattributed_ms, 2),
        }


def _rank(name: str) -> int:
    return _RANK.get(name, _UNKNOWN_RANK)


def compute(
    *,
    origin_ns: int,
    target_ns: int,
    spans: Sequence[PathSpan],
    min_segment_ms: float = 0.05,
) -> CriticalPath:
    """Partition ``[origin_ns, target_ns]`` across the stages that were active.

    ``min_segment_ms`` folds away boundary slivers created by two spans
    starting in the same microsecond; they are noise on a waterfall, and the
    duration they carry is added to the neighbouring segment so the partition
    stays exact.
    """
    if target_ns <= origin_ns:
        return CriticalPath(0.0, (), UNATTRIBUTED, 0.0, 0.0)

    clipped: list[PathSpan] = []
    for span in spans:
        end = span.end_ns if span.end_ns is not None else target_ns
        start = max(span.start_ns, origin_ns)
        end = min(end, target_ns)
        if end <= start:
            continue
        clipped.append(
            PathSpan(
                name=span.name,
                start_ns=start,
                end_ns=end,
                span_id=span.span_id,
                status=span.status,
                attempt=span.attempt,
                label=span.label,
            )
        )

    boundaries = {origin_ns, target_ns}
    for span in clipped:
        boundaries.add(span.start_ns)
        if span.end_ns is not None:
            boundaries.add(span.end_ns)
    cuts = sorted(b for b in boundaries if origin_ns <= b <= target_ns)

    raw: list[PathSegment] = []
    for left, right in zip(cuts, cuts[1:], strict=False):
        if right <= left:
            continue
        active = [s for s in clipped if s.start_ns <= left and (s.end_ns or target_ns) >= right]
        if active:
            # Deepest stage wins; ties break on the later start (the inner one).
            winner = max(active, key=lambda s: (_rank(s.name), s.start_ns))
            stage, span_id, label, status = winner.name, winner.span_id, winner.label, winner.status
        else:
            stage, span_id, label, status = UNATTRIBUTED, "", "", "ok"
        raw.append(
            PathSegment(
                stage=stage,
                start_offset_ms=(left - origin_ns) / 1_000_000.0,
                duration_ms=(right - left) / 1_000_000.0,
                span_id=span_id,
                label=label,
                status=status,
            )
        )

    merged = _merge(raw, min_segment_ms)
    total_ms = (target_ns - origin_ns) / 1_000_000.0

    per_stage: dict[str, float] = {}
    for segment in merged:
        per_stage[segment.stage] = per_stage.get(segment.stage, 0.0) + segment.duration_ms
    attributed = {k: v for k, v in per_stage.items() if k != UNATTRIBUTED}
    if attributed:
        bottleneck_stage = max(attributed, key=lambda k: attributed[k])
        bottleneck_ms = attributed[bottleneck_stage]
    else:
        bottleneck_stage, bottleneck_ms = UNATTRIBUTED, per_stage.get(UNATTRIBUTED, 0.0)

    return CriticalPath(
        total_ms=total_ms,
        segments=tuple(merged),
        bottleneck_stage=bottleneck_stage,
        bottleneck_ms=bottleneck_ms,
        unattributed_ms=per_stage.get(UNATTRIBUTED, 0.0),
    )


def _merge(segments: list[PathSegment], min_segment_ms: float) -> list[PathSegment]:
    """Join adjacent same-stage segments and absorb slivers.

    Absorption preserves the partition: a dropped sliver's duration is handed
    to its neighbour rather than discarded, so the segments still sum to the
    window.
    """
    out: list[PathSegment] = []
    for segment in segments:
        if out and out[-1].stage == segment.stage and out[-1].span_id == segment.span_id:
            previous = out[-1]
            out[-1] = PathSegment(
                stage=previous.stage,
                start_offset_ms=previous.start_offset_ms,
                duration_ms=previous.duration_ms + segment.duration_ms,
                span_id=previous.span_id,
                label=previous.label,
                status=previous.status,
            )
            continue
        if out and segment.duration_ms < min_segment_ms:
            previous = out[-1]
            out[-1] = PathSegment(
                stage=previous.stage,
                start_offset_ms=previous.start_offset_ms,
                duration_ms=previous.duration_ms + segment.duration_ms,
                span_id=previous.span_id,
                label=previous.label,
                status=previous.status,
            )
            continue
        out.append(segment)
    return out
