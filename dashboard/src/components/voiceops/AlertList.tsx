import type { OpsAlert } from "@/api/types"
import { cn } from "@/lib/utils"

export function AlertList({ alerts }: { alerts: OpsAlert[] }) {
  if (alerts.length === 0) {
    return (
      <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 p-4 text-sm text-emerald-400">
        No alerts firing.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {alerts.map((alert) => {
        const page = alert.severity === "page"
        return (
          <div
            key={alert.rule}
            className={cn(
              "rounded-xl border p-4",
              page ? "border-red-500/40 bg-red-500/5" : "border-amber-500/35 bg-amber-500/5",
            )}
          >
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                  page ? "bg-red-500/20 text-red-300" : "bg-amber-500/20 text-amber-300",
                )}
              >
                {alert.severity}
              </span>
              <span className={cn("text-sm font-medium", page ? "text-red-300" : "text-amber-300")}>
                {alert.title}
              </span>
            </div>
            <p className="mt-1.5 text-xs text-[var(--color-muted)]">{alert.detail}</p>
            {alert.runbook && (
              <p className="mt-1 font-mono text-[11px] text-[var(--color-muted)]">{alert.runbook}</p>
            )}
          </div>
        )
      })}
    </div>
  )
}
