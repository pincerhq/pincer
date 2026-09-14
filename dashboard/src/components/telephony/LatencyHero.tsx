import { useMemo, useState } from "react"
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import type { TelephonyMetricDefinition, TelephonyOverview } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { cn } from "@/lib/utils"
import { ExportMenu } from "./ExportMenu"
import { InfoHint, MetricBoundaries } from "./InfoHint"
import { Block, formatMs } from "./shared"

const SERIES = [
  { key: "p50", label: "p50", color: CHART_COLORS.primary },
  { key: "p95", label: "p95", color: CHART_COLORS.tertiary },
  { key: "p99", label: "p99", color: CHART_COLORS.quaternary },
] as const

type SeriesKey = (typeof SERIES)[number]["key"]

/**
 * Response latency: the headline number and how it moved.
 *
 * Each trend point is a percentile of that bucket's own turns. Percentiles do
 * not average, so a rolled-up "mean of p95s" would be a number about nothing —
 * the buckets are computed independently and drawn independently.
 */
export function LatencyHero({
  data,
  definition,
  query,
}: {
  data: TelephonyOverview
  definition?: TelephonyMetricDefinition
  query: Record<string, string>
}) {
  const [visible, setVisible] = useState<SeriesKey[]>(["p50", "p95"])
  const summary = data.stages.response_latency_ms

  const points = useMemo(
    () =>
      data.trend.map((point) => ({
        t: new Date(point.bucket_start).toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }),
        p50: point.p50,
        p95: point.p95,
        p99: point.p99,
        turns: point.turns,
      })),
    [data.trend],
  )

  const toggle = (key: SeriesKey) =>
    setVisible((current) =>
      current.includes(key) ? current.filter((k) => k !== key) : [...current, key],
    )

  return (
    <Block
      title="Response latency"
      info={
        definition ? (
          <>
            <MetricBoundaries
              start={definition.start_event}
              end={definition.end_event}
              source={definition.source}
              limitations={definition.limitations}
            />
            <p className="text-amber-400/90">
              This is audio SENT to the provider, not audio the caller heard. Caller-perceived
              latency needs an audio probe on the PSTN leg and is not measurable here.
            </p>
          </>
        ) : (
          <p>Caller speech end → first response audio sent to the provider.</p>
        )
      }
      actions={
        <ExportMenu
          dataset="turns"
          query={query}
          local={{
            label: "This chart",
            filename: "telephony-latency-trend",
            columns: ["t", "p50", "p95", "p99", "turns"],
            rows: points,
          }}
        />
      }
    >
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        {SERIES.map((series) => (
          <button
            key={series.key}
            onClick={() => toggle(series.key)}
            className={cn(
              "group text-left transition-opacity",
              visible.includes(series.key) ? "opacity-100" : "opacity-35 hover:opacity-70",
            )}
          >
            <div className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: series.color }} />
              <span className="text-[10px] uppercase tracking-wide text-[var(--color-muted)]">
                {series.label}
              </span>
            </div>
            <div className="text-2xl font-semibold tabular-nums">
              {summary?.[series.key] === null || summary?.[series.key] === undefined
                ? "—"
                : formatMs(summary[series.key] as number)}
            </div>
          </button>
        ))}

        <div className="ml-auto flex items-center gap-1.5 pb-1 text-[11px] text-[var(--color-muted)]">
          <span className={cn(!summary?.sufficient_samples && "text-amber-400")}>
            n={summary?.count ?? 0}
          </span>
          {summary && !summary.sufficient_samples && (
            <InfoHint title="Under-sampled">
              <p>
                Fewer than {summary.min_samples} turns in this window. Percentiles over a handful of
                samples move for reasons that have nothing to do with the pipeline — treat these as
                indicative, and note that alert rules will not fire below this threshold either.
              </p>
            </InfoHint>
          )}
        </div>
      </div>

      <div className="mt-3">
        {points.length ? (
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={points} margin={{ top: 6, right: 12, bottom: 0, left: -4 }}>
              <defs>
                {SERIES.map((series) => (
                  <linearGradient key={series.key} id={`fill-${series.key}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={series.color} stopOpacity={0.28} />
                    <stop offset="100%" stopColor={series.color} stopOpacity={0} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid {...CHART_THEME.grid} vertical={false} />
              <XAxis dataKey="t" {...CHART_THEME.axis} minTickGap={32} />
              <YAxis {...CHART_THEME.axis} width={62} tickMargin={4} tickFormatter={(v: number) => formatMs(v)} />
              <Tooltip
                contentStyle={CHART_THEME.tooltip}
                formatter={(value, name) => [
                  name === "turns" || typeof value !== "number" ? String(value ?? "—") : formatMs(value),
                  String(name),
                ]}
              />
              <Legend
                verticalAlign="top"
                height={24}
                iconType="square"
                wrapperStyle={{ fontSize: 11, cursor: "pointer" }}
                onClick={(entry) => toggle(String(entry.dataKey) as SeriesKey)}
              />
              {SERIES.filter((series) => visible.includes(series.key)).map((series) => (
                <Area
                  key={series.key}
                  type="monotone"
                  dataKey={series.key}
                  stroke={series.color}
                  strokeWidth={2}
                  fill={`url(#fill-${series.key})`}
                  connectNulls
                  dot={false}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <p className="py-12 text-center text-xs text-[var(--color-muted)]">
            No turns in this window.
          </p>
        )}
      </div>
    </Block>
  )
}
