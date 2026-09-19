import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import type { TelephonyMetricDefinition, TelephonyOverview } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { ExportMenu } from "./ExportMenu"
import { InfoHint, MetricBoundaries } from "./InfoHint"
import { Block, METRIC_LABELS, formatMs } from "./shared"

/**
 * Per-stage p50/p95 as a bar chart you click to drill into.
 *
 * These bars are NOT a breakdown of the total: the stages overlap (the model is
 * still writing while the first sentence is already being synthesised), so they
 * are drawn side by side and never stacked. The non-overlapping attribution
 * lives on each turn's critical path.
 */
export function StageBars({
  data,
  definitions,
  selected,
  onSelect,
  query,
}: {
  data: TelephonyOverview
  definitions: TelephonyMetricDefinition[]
  selected: string
  onSelect: (stage: string) => void
  query: Record<string, string>
}) {
  const byKey = new Map(definitions.map((d) => [d.key, d]))

  const inScope = Object.entries(data.stages).filter(
    ([key, summary]) => summary.count > 0 && key !== "total_ms",
  )

  // A null percentile is "not measured" and must never be drawn as a 0 ms bar:
  // `Ms` in ./shared states the rule — rendering a missing number as zero makes
  // the least observable stage look like the fastest. `?? 0` did exactly that,
  // and no amount of dimming fixes it, because the bar's LENGTH is the lie.
  //
  // Reachability, so the next reader does not go looking for a broken screen:
  // `LatencyHistogram.percentile` only returns None when `count == 0`, which
  // the filter above already drops, so this is currently unreachable from the
  // backend. It is held here because `LatencySummary.p50` is `number | null` on
  // this side of the wire — the guarantee lives in Python and nothing carries
  // it across, so a change to `min_samples` or a new summary source would
  // reintroduce the silent 0 ms bar rather than a visible gap.
  const unmeasured = inScope
    .filter(([, summary]) => summary.p50 === null || summary.p95 === null)
    .map(([key]) => METRIC_LABELS[key] ?? key)

  const rows = inScope
    .flatMap(([key, summary]) => {
      const { p50, p95 } = summary
      if (p50 === null || p95 === null) return []
      return [
        {
          key,
          label: METRIC_LABELS[key] ?? key,
          p50,
          p95,
          count: summary.count,
          enough: summary.sufficient_samples,
        },
      ]
    })
    .sort((a, b) => b.p95 - a.p95)

  // Negative durations the aggregator refused (a clock-ordering defect). Read
  // off every stage, not `rows`: refused observations are not in `count`, so a
  // stage where ALL of them were refused has count 0 and is filtered out above.
  const refused = Object.entries(data.stages)
    .filter(([, summary]) => (summary.invalid ?? 0) > 0)
    .map(([key, summary]) => `${METRIC_LABELS[key] ?? key} ${summary.invalid}`)

  const unavailable = data.unavailable.filter((metric) => metric.available_on.length > 0)

  return (
    <Block
      title="Where the time goes"
      info={
        <>
          <p>
            Per-stage duration percentiles. <strong>These overlap and are never summed</strong> — a
            streaming turn runs the LLM, tools and TTS concurrently, so adding the bars would
            double-count and misname the bottleneck.
          </p>
          <p>Click a bar to see that stage's distribution.</p>
        </>
      }
      actions={
        <>
          {unavailable.length > 0 && (
            <InfoHint title="Not measurable here" label="Unavailable metrics">
              <p>
                These stages cannot be observed for the engines in this window, so they are absent
                rather than zero:
              </p>
              <ul className="space-y-1">
                {unavailable.map((metric) => (
                  <li key={metric.key}>
                    <span className="text-[var(--color-foreground)]">{metric.label}</span> —{" "}
                    {metric.unavailable_reason}
                  </li>
                ))}
              </ul>
            </InfoHint>
          )}
          <ExportMenu
            dataset="stages"
            query={query}
            local={{
              label: "This chart",
              filename: "telephony-stages",
              columns: ["key", "label", "p50", "p95", "count", "enough"],
              rows,
            }}
          />
        </>
      }
    >
      {refused.length > 0 && (
        <p className="mb-2 text-[10px] text-amber-400">
          Refused as impossible (negative duration, a clock-ordering defect): {refused.join(", ")}. These
          are excluded from the percentiles, not counted as 0ms.
        </p>
      )}
      {rows.length ? (
        <>
          <ResponsiveContainer width="100%" height={Math.max(200, rows.length * 36)}>
            <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 28, bottom: 0, left: 4 }} barGap={2}>
              <XAxis type="number" {...CHART_THEME.axis} tickFormatter={(v: number) => formatMs(v)} />
              <YAxis
                type="category"
                dataKey="label"
                width={172}
                {...CHART_THEME.axis}
                tick={{ fontSize: 10, fill: "#9a9a9a" }}
                interval={0}
              />
              <Tooltip
                cursor={{ fill: "rgba(255,255,255,0.04)" }}
                contentStyle={CHART_THEME.tooltip}
                formatter={(value, name) => [
                  typeof value === "number" ? formatMs(value) : String(value),
                  String(name),
                ]}
              />
              <Bar dataKey="p50" name="p50" radius={[0, 3, 3, 0]} onClick={(row) => onSelect(String(row.key ?? ""))}>
                {rows.map((row) => (
                  <Cell
                    key={row.key}
                    cursor="pointer"
                    fill={CHART_COLORS.primary}
                    fillOpacity={selected === row.key ? 1 : 0.55}
                  />
                ))}
              </Bar>
              <Bar dataKey="p95" name="p95" radius={[0, 3, 3, 0]} onClick={(row) => onSelect(String(row.key ?? ""))}>
                {rows.map((row) => (
                  <Cell
                    key={row.key}
                    cursor="pointer"
                    fill={CHART_COLORS.tertiary}
                    fillOpacity={selected === row.key ? 1 : 0.55}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>

          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-[var(--color-muted)]">
            {rows.map((row) => {
              const definition = byKey.get(row.key)
              return (
                <button
                  key={row.key}
                  onClick={() => onSelect(row.key)}
                  className={`inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-white/[0.05] ${
                    selected === row.key ? "text-[var(--color-foreground)]" : ""
                  }`}
                >
                  {row.label}
                  <span className={row.enough ? "" : "text-amber-400"}>n={row.count}</span>
                  {definition && (
                    <InfoHint title={definition.label}>
                      <MetricBoundaries
                        start={definition.start_event}
                        end={definition.end_event}
                        source={definition.source}
                        limitations={definition.limitations}
                      />
                    </InfoHint>
                  )}
                </button>
              )
            })}
          </div>
        </>
      ) : (
        <p className="py-10 text-center text-xs text-[var(--color-muted)]">
          No stage measurements in this window.
        </p>
      )}
      {unmeasured.length > 0 && (
        <p className="mt-1.5 text-[10px] text-[var(--color-muted)]">
          Not measured: {unmeasured.join(", ")} — these stages ran but report no percentile, so
          they get no bar rather than a 0ms one.
        </p>
      )}
    </Block>
  )
}
