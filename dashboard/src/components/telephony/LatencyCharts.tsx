import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import type { HistogramBucket, TelephonyOverview } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { METRIC_LABELS, formatMs } from "./shared"

/** Bucketed distribution for one stage. The last bucket is the overflow. */
export function DistributionChart({
  buckets,
  stage,
}: {
  buckets: HistogramBucket[] | undefined
  stage: string
}) {
  if (!buckets || buckets.every((b) => b.count === 0)) {
    return (
      <p className="py-8 text-center text-xs text-[var(--color-muted)]">
        No observations for {METRIC_LABELS[stage] ?? stage} in this window.
      </p>
    )
  }
  const data = buckets
    .filter((b) => b.count > 0)
    .map((b) => ({
      label: b.upper_ms === null ? `>${formatMs(b.lower_ms)}` : formatMs(b.upper_ms),
      range: b.upper_ms === null ? `above ${formatMs(b.lower_ms)}` : `${formatMs(b.lower_ms)} – ${formatMs(b.upper_ms)}`,
      count: b.count,
    }))
  return (
    <ResponsiveContainer width="100%" height={216}>
      <BarChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -8 }}>
        <CartesianGrid {...CHART_THEME.grid} vertical={false} />
        <XAxis dataKey="label" {...CHART_THEME.axis} minTickGap={8} tickMargin={6} />
        <YAxis {...CHART_THEME.axis} allowDecimals={false} width={40} />
        <Tooltip
          contentStyle={CHART_THEME.tooltip}
          cursor={{ fill: "rgba(255,255,255,0.04)" }}
          labelFormatter={(_label, payload) => payload?.[0]?.payload?.range ?? ""}
          formatter={(value) => [String(value), "turns"]}
        />
        <Bar dataKey="count" fill={CHART_COLORS.secondary} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}

/**
 * Response-latency percentiles over time.
 *
 * Each point is computed from that bucket's own raw turns — never a mean of
 * neighbouring percentiles, which would not be a percentile of anything.
 */
export function LatencyTrend({ trend }: { trend: TelephonyOverview["trend"] }) {
  if (!trend.length) {
    return <p className="py-8 text-center text-xs text-[var(--color-muted)]">No turns in this window.</p>
  }
  const data = trend.map((point) => ({
    t: new Date(point.bucket_start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    p50: point.p50,
    p95: point.p95,
    turns: point.turns,
  }))
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid {...CHART_THEME.grid} vertical={false} />
        <XAxis dataKey="t" {...CHART_THEME.axis} />
        <YAxis {...CHART_THEME.axis} unit="ms" />
        <Tooltip
          contentStyle={CHART_THEME.tooltip}
          formatter={(value, name) => [
            typeof value === "number" ? formatMs(value) : String(value ?? "—"),
            String(name),
          ]}
        />
        <Line type="monotone" dataKey="p50" stroke={CHART_COLORS.primary} dot={false} strokeWidth={2} />
        <Line type="monotone" dataKey="p95" stroke={CHART_COLORS.tertiary} dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  )
}
