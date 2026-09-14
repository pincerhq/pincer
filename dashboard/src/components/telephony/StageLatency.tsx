import type { TelephonyMetricDefinition, TelephonyOverview } from "@/api/types"
import { METRIC_LABELS, Ms, SampleNote, SourceBadge } from "./shared"

/**
 * p50 / p95 / p99 per pipeline stage, each with its sample count.
 *
 * These are per-stage DURATIONS, and several of them overlap in time (the LLM
 * is still writing while TTS synthesises). They are deliberately not stacked
 * or summed anywhere — the only place stage time is added up is the per-turn
 * critical path, which partitions the window instead of summing intervals.
 */
export function StageLatency({
  data,
  definitions,
  onSelect,
  selected,
}: {
  data: TelephonyOverview
  definitions: TelephonyMetricDefinition[]
  onSelect: (stage: string) => void
  selected: string
}) {
  const byKey = new Map(definitions.map((d) => [d.key, d]))
  const rows = Object.entries(data.stages)

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-[var(--color-border)] text-left text-[11px] text-[var(--color-muted)]">
            <th className="py-2 pr-3 font-medium">Stage</th>
            <th className="py-2 pr-3 text-right font-medium">p50</th>
            <th className="py-2 pr-3 text-right font-medium">p95</th>
            <th className="py-2 pr-3 text-right font-medium">p99</th>
            <th className="py-2 pr-3 text-right font-medium">max</th>
            <th className="py-2 pr-3 font-medium">samples</th>
            <th className="py-2 font-medium">source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, summary]) => {
            const def = byKey.get(key)
            const isSelected = selected === key
            return (
              <tr
                key={key}
                onClick={() => onSelect(key)}
                className={`cursor-pointer border-b border-[var(--color-border)]/60 hover:bg-white/[0.03] ${
                  isSelected ? "bg-white/[0.05]" : ""
                }`}
              >
                <td className="py-2 pr-3">
                  <div className="font-medium">{METRIC_LABELS[key] ?? key}</div>
                  {def?.limitations && (
                    <div className="mt-0.5 max-w-[42ch] text-[10px] leading-snug text-[var(--color-muted)]">
                      {def.limitations}
                    </div>
                  )}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <Ms value={summary.p50} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <Ms value={summary.p95} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <Ms value={summary.p99} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-[var(--color-muted)]">
                  <Ms value={summary.max} />
                </td>
                <td className="py-2 pr-3">
                  <SampleNote summary={summary} />
                </td>
                <td className="py-2">{def && <SourceBadge source={def.source} />}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
