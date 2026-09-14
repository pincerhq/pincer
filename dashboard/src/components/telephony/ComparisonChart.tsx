import { useState } from "react"
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import type { TelephonyFilters, TelephonyOverview } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { cn } from "@/lib/utils"
import { ExportMenu } from "./ExportMenu"
import { Block, formatMs } from "./shared"

const GROUPS = [
  { key: "engine", label: "Engine", filterKey: "engine" },
  { key: "model", label: "Model", filterKey: "model" },
  { key: "direction", label: "Direction", filterKey: "direction" },
  { key: "provider", label: "Provider", filterKey: "provider" },
] as const

/**
 * Response latency per engine / model / direction / provider.
 *
 * Each bar comes off that group's own histogram — nothing here averages another
 * percentile. Groups that are too small to compare are dimmed and labelled,
 * because two bars of different heights invite a conclusion the sample size may
 * not support.
 */
export function ComparisonChart({
  data,
  filters,
  onChange,
  query,
}: {
  data: TelephonyOverview
  filters: TelephonyFilters
  onChange: (next: TelephonyFilters) => void
  query: Record<string, string>
}) {
  const [group, setGroup] = useState<(typeof GROUPS)[number]["key"]>("engine")
  const active = GROUPS.find((g) => g.key === group)!
  const rows = (data.comparisons[group] ?? [])
    .filter((row) => row.count > 0)
    .map((row) => ({
      key: row.key,
      p50: row.p50 ?? 0,
      p95: row.p95 ?? 0,
      count: row.count,
      enough: row.sufficient_samples,
    }))

  const comparable = rows.filter((row) => row.enough).length

  return (
    <Block
      title="Compare"
      info={
        <>
          <p>
            Response-latency percentiles per group, each computed from that group's own histogram.
            Percentiles are never averaged across groups.
          </p>
          <p>
            Bars are only comparable when both groups have enough samples — dimmed bars are below the
            threshold and their difference is probably noise.
          </p>
          <p>Click a bar to filter the page to that group.</p>
        </>
      }
      actions={
        <ExportMenu
          local={{
            label: "This chart",
            filename: `telephony-compare-${group}`,
            columns: ["key", "p50", "p95", "count", "enough"],
            rows,
          }}
          query={query}
        />
      }
    >
      <div className="mb-2 flex flex-wrap gap-1">
        {GROUPS.map((option) => (
          <button
            key={option.key}
            onClick={() => setGroup(option.key)}
            className={cn(
              "rounded-md px-2 py-1 text-[11px] transition-colors",
              group === option.key
                ? "bg-white/[0.08] text-[var(--color-foreground)]"
                : "text-[var(--color-muted)] hover:bg-white/[0.04]",
            )}
          >
            {option.label}
          </button>
        ))}
        {rows.length > 1 && comparable < 2 && (
          <span className="ml-auto self-center text-[10px] text-amber-400">
            not enough samples to compare
          </span>
        )}
      </div>

      {rows.length ? (
        <ResponsiveContainer width="100%" height={Math.max(160, rows.length * 48)}>
          <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 32, bottom: 0, left: 4 }} barGap={2}>
            <XAxis type="number" {...CHART_THEME.axis} tickFormatter={(v: number) => formatMs(v)} />
            <YAxis
              type="category"
              dataKey="key"
              width={150}
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
            {(["p50", "p95"] as const).map((series, index) => (
              <Bar
                key={series}
                dataKey={series}
                name={series}
                radius={[0, 3, 3, 0]}
                onClick={(row) => {
                  const value = String(row.key ?? "")
                  onChange({
                    ...filters,
                    [active.filterKey]:
                      filters[active.filterKey as keyof TelephonyFilters] === value ? undefined : value,
                  })
                }}
              >
                {rows.map((row) => (
                  <Cell
                    key={row.key}
                    cursor="pointer"
                    fill={index === 0 ? CHART_COLORS.primary : CHART_COLORS.tertiary}
                    fillOpacity={row.enough ? 1 : 0.4}
                  />
                ))}
              </Bar>
            ))}
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <p className="py-10 text-center text-xs text-[var(--color-muted)]">Nothing to compare yet.</p>
      )}
    </Block>
  )
}
