import { Link } from "react-router-dom"
import type { TelephonyCall } from "@/api/types"
import { ROUTES } from "@/lib/constants"
import { cn } from "@/lib/utils"
import { Ms } from "./shared"

const SORTABLE = ["registered_at", "duration_ms", "turn_count", "setup_ms", "status"] as const

function statusTone(call: TelephonyCall): string {
  if (call.status === "active") return "text-sky-400"
  if (call.failure_category === "technical") return "text-red-400"
  if (call.failure_category === "callee_unavailable") return "text-amber-400"
  if (call.failure_category === "policy_declined") return "text-[var(--color-muted)]"
  return "text-emerald-400"
}

export function CallTable({
  calls,
  total,
  offset,
  limit,
  sort,
  order,
  onSort,
  onPage,
}: {
  calls: TelephonyCall[]
  total: number
  offset: number
  limit: number
  sort: string
  order: string
  onSort: (column: string) => void
  onPage: (offset: number) => void
}) {
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-left text-[11px] text-[var(--color-muted)]">
              <th className="py-2 pr-3 font-medium">Call</th>
              <th className="py-2 pr-3 font-medium">Dir</th>
              <th className="py-2 pr-3 font-medium">Engine</th>
              {SORTABLE.slice(0, 1).map((col) => (
                <th key={col} className="py-2 pr-3 font-medium">
                  <button onClick={() => onSort(col)} className="hover:text-[var(--color-foreground)]">
                    Started {sort === col && (order === "desc" ? "↓" : "↑")}
                  </button>
                </th>
              ))}
              <th className="py-2 pr-3 text-right font-medium">
                <button onClick={() => onSort("setup_ms")} className="hover:text-[var(--color-foreground)]">
                  Setup {sort === "setup_ms" && (order === "desc" ? "↓" : "↑")}
                </button>
              </th>
              <th className="py-2 pr-3 text-right font-medium">
                <button onClick={() => onSort("duration_ms")} className="hover:text-[var(--color-foreground)]">
                  Duration {sort === "duration_ms" && (order === "desc" ? "↓" : "↑")}
                </button>
              </th>
              <th className="py-2 pr-3 text-right font-medium">
                <button onClick={() => onSort("turn_count")} className="hover:text-[var(--color-foreground)]">
                  Turns {sort === "turn_count" && (order === "desc" ? "↓" : "↑")}
                </button>
              </th>
              <th className="py-2 pr-3 font-medium">Status</th>
              <th className="py-2 font-medium">Telemetry</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((call) => (
              <tr key={call.call_id} className="border-b border-[var(--color-border)]/60 hover:bg-white/[0.03]">
                <td className="py-2 pr-3">
                  <Link
                    to={ROUTES.TELEPHONY_CALL.replace(":callRef", call.provider_call_id || call.call_id)}
                    className="font-mono text-xs text-[var(--color-accent)] hover:underline"
                  >
                    {call.provider_call_id || call.call_id.slice(0, 12)}
                  </Link>
                  <div className="mt-0.5 font-mono text-[10px] text-[var(--color-muted)]">
                    {call.direction === "outbound" ? call.to_number_masked : call.from_number_masked}
                  </div>
                </td>
                <td className="py-2 pr-3 text-xs">{call.direction}</td>
                <td className="py-2 pr-3 font-mono text-[11px]">{call.engine || "—"}</td>
                <td className="py-2 pr-3 text-xs text-[var(--color-muted)]">
                  {call.registered_at ? new Date(call.registered_at).toLocaleString() : "—"}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <Ms value={call.setup_ms} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <Ms value={call.duration_ms} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">{call.turn_count}</td>
                <td className={cn("py-2 pr-3 text-xs", statusTone(call))}>
                  {call.status}
                  {call.failure_code && call.failure_code !== "none" && (
                    <div className="font-mono text-[10px] opacity-80">{call.failure_code}</div>
                  )}
                </td>
                <td className="py-2 text-[10px] text-[var(--color-muted)]">
                  {call.sampled ? call.coverage : "not sampled"}
                </td>
              </tr>
            ))}
            {!calls.length && (
              <tr>
                <td colSpan={9} className="py-8 text-center text-xs text-[var(--color-muted)]">
                  No calls match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-3 text-[11px] text-[var(--color-muted)]">
        <span>
          {total === 0 ? 0 : offset + 1}–{Math.min(offset + limit, total)} of {total}
        </span>
        <button
          disabled={offset === 0}
          onClick={() => onPage(Math.max(0, offset - limit))}
          className="rounded px-2 py-1 hover:bg-white/[0.06] disabled:opacity-40"
        >
          Previous
        </button>
        <button
          disabled={offset + limit >= total}
          onClick={() => onPage(offset + limit)}
          className="rounded px-2 py-1 hover:bg-white/[0.06] disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  )
}
