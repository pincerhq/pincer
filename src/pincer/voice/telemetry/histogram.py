"""
Histogram aggregation for latency percentiles.

Percentiles do not average. Taking p95 per hour and then meaning the hours, or
taking p95 per provider and then meaning the providers, produces a number that
is not a percentile of anything. So every aggregate in this subsystem is built
by feeding raw observations into a fixed-bucket histogram and reading the
percentile off the merged histogram — merging histograms is exact in the sense
that matters (it is the same as having pooled the observations first, up to
bucket width).

Bucket edges are explicit and sub-second-dense because that is where voice
latency lives; the defaults share their lower half with
`observability/metrics.STAGE_LATENCY_BUCKETS` so a number read off Grafana and
a number read off this dashboard land in comparable buckets.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

#: Milliseconds. Dense under 1 s, coarse above it.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    10.0,
    25.0,
    50.0,
    75.0,
    100.0,
    150.0,
    200.0,
    300.0,
    400.0,
    600.0,
    800.0,
    1000.0,
    1250.0,
    1500.0,
    2000.0,
    2500.0,
    3000.0,
    4000.0,
    6000.0,
    8000.0,
    12000.0,
    20000.0,
)


@dataclass
class LatencyHistogram:
    """Fixed-bucket histogram with exact min/max and interpolated percentiles."""

    buckets: tuple[float, ...] = DEFAULT_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    overflow: int = 0
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def add(self, value: float | None) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if v != v:  # NaN
            return
        v = max(0.0, v)
        self.count += 1
        self.total += v
        self.minimum = v if self.minimum is None else min(self.minimum, v)
        self.maximum = v if self.maximum is None else max(self.maximum, v)
        index = bisect_left(self.buckets, v)
        if index >= len(self.buckets):
            self.overflow += 1
        else:
            self.counts[index] += 1

    def merge(self, other: LatencyHistogram) -> None:
        if other.buckets != self.buckets:
            raise ValueError("cannot merge histograms with different bucket edges")
        self.counts = [a + b for a, b in zip(self.counts, other.counts, strict=True)]
        self.overflow += other.overflow
        self.count += other.count
        self.total += other.total
        if other.minimum is not None:
            self.minimum = other.minimum if self.minimum is None else min(self.minimum, other.minimum)
        if other.maximum is not None:
            self.maximum = other.maximum if self.maximum is None else max(self.maximum, other.maximum)

    def percentile(self, pct: float) -> float | None:
        """Linear-interpolated percentile within the containing bucket.

        ``None`` when there are no observations: an empty percentile is not
        zero, and rendering it as zero is how a dashboard claims a broken
        pipeline is the fastest one.
        """
        if self.count == 0:
            return None
        target = pct * self.count
        cumulative = 0
        lower = 0.0
        for index, edge in enumerate(self.buckets):
            bucket_count = self.counts[index]
            if bucket_count and cumulative + bucket_count >= target:
                within = (target - cumulative) / bucket_count
                return lower + (edge - lower) * min(1.0, max(0.0, within))
            cumulative += bucket_count
            lower = edge
        # Everything left is above the last edge; the exact max is the honest
        # answer there rather than an extrapolation into an open bucket.
        return self.maximum

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    def summary(self, *, min_samples: int = 1) -> dict[str, object]:
        """The shape the API and UI consume.

        ``sufficient_samples`` is part of the payload so the dashboard can show
        "p99 over 7 samples" as the noise it is instead of as a fact.
        """
        return {
            "count": self.count,
            "p50": _round(self.percentile(0.50)),
            "p95": _round(self.percentile(0.95)),
            "p99": _round(self.percentile(0.99)),
            "min": _round(self.minimum),
            "max": _round(self.maximum),
            "mean": _round(self.mean),
            "sufficient_samples": self.count >= min_samples,
            "min_samples": min_samples,
        }

    def distribution(self) -> list[dict[str, object]]:
        """Bucket counts for the distribution chart, including the overflow."""
        rows: list[dict[str, object]] = []
        lower = 0.0
        for index, edge in enumerate(self.buckets):
            rows.append({"lower_ms": lower, "upper_ms": edge, "count": self.counts[index]})
            lower = edge
        rows.append({"lower_ms": lower, "upper_ms": None, "count": self.overflow})
        return rows


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def build(values: list[float | None], buckets: tuple[float, ...] = DEFAULT_BUCKETS_MS) -> LatencyHistogram:
    hist = LatencyHistogram(buckets=buckets)
    for value in values:
        hist.add(value)
    return hist
