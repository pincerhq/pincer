import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts"
import type { TelephonyCall, TelephonyFilters } from "@/api/types"
import { ExportMenu } from "./ExportMenu"
import { Block, TruncationNote } from "./shared"

const CATEGORY_META: Record<string, { label: string; color: string; explain: string }> = {
  none: { label: "Succeeded", color: "#10b981", explain: "The call did what it was supposed to." },
  technical: {
    label: "Technical failure",
    color: "#ef4444",
    explain: "Ours. Burns error budget and belongs on the technical failure rate.",
  },
  callee_unavailable: {
    label: "Callee unavailable",
    color: "#f59e0b",
    explain: "Busy, no answer, voicemail or wrong number. Healthy system, unavailable person.",
  },
  policy_declined: {
    label: "Declined by policy",
    color: "#6366f1",
    explain: "A guardrail said no — blocklist, capacity, quiet hours, do-not-call. The system worked.",
  },
  ended_by_party: {
    label: "Ended by a party",
    color: "#94a3b8",
    explain: "Someone hung up. Normal in conversation phases.",
  },
  unknown: { label: "Unclassified", color: "#475569", explain: "Could not be categorised; counted as ours until proven otherwise." },
}

/**
 * How calls ended, as a click-to-filter donut.
 *
 * The split matters more than the total: folding "the line was busy" into the
 * same bucket as "our media socket dropped" produces a failure rate that moves
 * on public holidays, and an alert nobody trusts.
 */
export function OutcomeMix({
  calls,
  total: matching,
  filters,
  onChange,
  query,
}: {
  calls: TelephonyCall[]
  /** Calls matching the filters, which is more than `calls` once past the cap. */
  total: number
  filters: TelephonyFilters
  onChange: (next: TelephonyFilters) => void
  query: Record<string, string>
}) {
  const counts = new Map<string, number>()
  for (const call of calls) {
    const key = call.failure_category || "unknown"
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  const rows = [...counts.entries()]
    .map(([category, count]) => ({
      category,
      label: CATEGORY_META[category]?.label ?? category,
      color: CATEGORY_META[category]?.color ?? "#475569",
      count,
    }))
    .sort((a, b) => b.count - a.count)

  const total = rows.reduce((sum, row) => sum + row.count, 0)

  return (
    <Block
      title="How calls ended"
      info={
        <>
          <p>Terminal category of each call counted here. Click a segment to filter the page.</p>
          <ul className="space-y-1">
            {Object.entries(CATEGORY_META).map(([key, meta]) => (
              <li key={key}>
                <span className="text-[var(--color-foreground)]">{meta.label}</span> — {meta.explain}
              </li>
            ))}
          </ul>
          <p className="opacity-70">
            Counted over the calls loaded for the table, so it follows the same filters. That
            list is capped at 500 rows, so on a busy window this is the most recent 500 rather
            than all of them — the note under the chart says so when it applies.
          </p>
        </>
      }
      actions={
        <ExportMenu
          dataset="calls"
          query={query}
          local={{
            label: "This chart",
            filename: "telephony-outcomes",
            columns: ["category", "label", "count"],
            rows,
          }}
        />
      }
    >
      {total ? (
        <div className="flex items-center gap-4">
          <ResponsiveContainer width="50%" height={170}>
            <PieChart>
              <Pie
                data={rows}
                dataKey="count"
                nameKey="label"
                innerRadius={44}
                outerRadius={70}
                paddingAngle={2}
                onClick={(entry) => {
                  const category = (entry as unknown as { category: string }).category
                  onChange({
                    ...filters,
                    failure_category: filters.failure_category === category ? undefined : category,
                  })
                }}
              >
                {rows.map((row) => (
                  <Cell
                    key={row.category}
                    fill={row.color}
                    cursor="pointer"
                    stroke="transparent"
                    fillOpacity={
                      !filters.failure_category || filters.failure_category === row.category ? 1 : 0.3
                    }
                  />
                ))}
              </Pie>
              <Tooltip contentStyle={CHART_TOOLTIP} />
            </PieChart>
          </ResponsiveContainer>

          <ul className="flex-1 space-y-1">
            {rows.map((row) => (
              <li key={row.category}>
                <button
                  onClick={() =>
                    onChange({
                      ...filters,
                      failure_category:
                        filters.failure_category === row.category ? undefined : row.category,
                    })
                  }
                  className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-[11px] hover:bg-white/[0.05]"
                >
                  <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: row.color }} />
                  <span className="truncate">{row.label}</span>
                  <span className="ml-auto tabular-nums">{row.count}</span>
                  <span className="w-10 text-right tabular-nums text-[var(--color-muted)]">
                    {((row.count / total) * 100).toFixed(0)}%
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="py-12 text-center text-xs text-[var(--color-muted)]">No calls in this window.</p>
      )}
      <TruncationNote loaded={calls.length} total={matching} />
    </Block>
  )
}

const CHART_TOOLTIP = {
  backgroundColor: "#1a1a1a",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8,
  fontSize: 11,
}
