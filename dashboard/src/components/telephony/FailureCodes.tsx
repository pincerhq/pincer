import type { TelephonyCall, TelephonyFilters } from "@/api/types"
import { cn } from "@/lib/utils"
import { ExportMenu } from "./ExportMenu"
import { Block, TruncationNote } from "./shared"

const CATEGORY_TONE: Record<string, string> = {
  technical: "text-red-400",
  callee_unavailable: "text-amber-400",
  policy_declined: "text-indigo-300",
  ended_by_party: "text-[var(--color-muted)]",
  none: "text-emerald-400",
}

/**
 * Which failure codes actually occurred, and how often.
 *
 * The outcome donut says *what kind* of thing went wrong; this says *which
 * one*, which is the next question and the one that maps onto a runbook
 * heading. Codes are the stable taxonomy shared by the metric labels, the
 * database column and the weekly digest — the same string everywhere.
 */
export function FailureCodes({
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
  const counts = new Map<string, { code: string; category: string; count: number; lastSeen: string }>()
  for (const call of calls) {
    const code = call.failure_code || "none"
    if (code === "none") continue
    const existing = counts.get(code)
    const seen = call.ended_at ?? call.registered_at ?? ""
    if (existing) {
      existing.count += 1
      if (seen > existing.lastSeen) existing.lastSeen = seen
    } else {
      counts.set(code, { code, category: call.failure_category || "unknown", count: 1, lastSeen: seen })
    }
  }

  const rows = [...counts.values()].sort((a, b) => b.count - a.count)

  return (
    <Block
      title="Failure codes"
      info={
        <>
          <p>
            The stable code each terminated call was tagged with. The same string is a metric label,
            a database column and a runbook heading, so it is the thing to search for.
          </p>
          <p>
            Successful calls (<span className="font-mono">none</span>) are excluded. Click a row to
            filter the page to that code. Counts come from the capped call list, so on a busy
            window they are the most recent 500 rather than every call — the note below says so
            when it applies.
          </p>
        </>
      }
      actions={<ExportMenu dataset="calls" query={query} />}
    >
      {rows.length ? (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-left text-[11px] text-[var(--color-muted)]">
              <th className="py-1.5 pr-3 font-medium">Code</th>
              <th className="py-1.5 pr-3 font-medium">Category</th>
              <th className="py-1.5 pr-3 text-right font-medium">Calls</th>
              <th className="py-1.5 font-medium">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const isActive = filters.failure_code === row.code
              return (
                <tr
                  key={row.code}
                  onClick={() =>
                    onChange({ ...filters, failure_code: isActive ? undefined : row.code })
                  }
                  className={cn(
                    "cursor-pointer border-b border-[var(--color-border)]/60 hover:bg-white/[0.03]",
                    isActive && "bg-white/[0.05]",
                  )}
                >
                  <td className="py-1.5 pr-3 font-mono text-xs">{row.code}</td>
                  <td className={cn("py-1.5 pr-3 text-[11px]", CATEGORY_TONE[row.category] ?? "")}>
                    {row.category.replace(/_/g, " ")}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{row.count}</td>
                  <td className="py-1.5 text-[11px] text-[var(--color-muted)]">
                    {row.lastSeen ? new Date(row.lastSeen).toLocaleString() : "—"}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        <p className="py-8 text-center text-xs text-[var(--color-muted)]">
          No terminated call in this window carried a failure code.
        </p>
      )}
      <TruncationNote loaded={calls.length} total={matching} />
    </Block>
  )
}
