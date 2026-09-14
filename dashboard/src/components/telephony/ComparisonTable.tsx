import type { TelephonyOverview } from "@/api/types"
import { Ms, SampleNote } from "./shared"

const GROUP_LABELS: Record<string, string> = {
  engine: "Engine",
  model: "Model",
  provider: "Provider",
  direction: "Direction",
}

/**
 * Response latency by engine / model / provider / direction.
 *
 * Each row's percentiles come off that group's own histogram. Two rows are only
 * comparable when both have enough samples, which is why the sample count sits
 * next to every number rather than in a footnote.
 */
export function ComparisonTable({ comparisons }: { comparisons: TelephonyOverview["comparisons"] }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {Object.entries(comparisons).map(([group, rows]) => {
        const populated = rows.filter((row) => row.count > 0)
        if (!populated.length) return null
        const comparable = populated.filter((row) => row.sufficient_samples)
        return (
          <div key={group}>
            <h3 className="mb-1.5 text-xs font-medium text-[var(--color-muted)]">
              {GROUP_LABELS[group] ?? group}
            </h3>
            {populated.length > 1 && comparable.length < 2 && (
              <p className="mb-1.5 text-[10px] text-amber-400">
                Not enough samples to compare these groups — treat the difference as noise.
              </p>
            )}
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border)] text-left text-[11px] text-[var(--color-muted)]">
                  <th className="py-1.5 pr-2 font-medium">{GROUP_LABELS[group] ?? group}</th>
                  <th className="py-1.5 pr-2 text-right font-medium">p50</th>
                  <th className="py-1.5 pr-2 text-right font-medium">p95</th>
                  <th className="py-1.5 font-medium">samples</th>
                </tr>
              </thead>
              <tbody>
                {populated.map((row) => (
                  <tr key={row.key} className="border-b border-[var(--color-border)]/60">
                    <td className="py-1.5 pr-2 font-mono text-xs">{row.key}</td>
                    <td className="py-1.5 pr-2 text-right tabular-nums">
                      <Ms value={row.p50} />
                    </td>
                    <td className="py-1.5 pr-2 text-right tabular-nums">
                      <Ms value={row.p95} />
                    </td>
                    <td className="py-1.5">
                      <SampleNote summary={row} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      })}
    </div>
  )
}
