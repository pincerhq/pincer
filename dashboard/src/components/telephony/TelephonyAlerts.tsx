import { Link } from "react-router-dom"
import type { TelephonyAlert } from "@/api/types"
import { ROUTES } from "@/lib/constants"
import { cn } from "@/lib/utils"

function severityStyle(alert: TelephonyAlert): string {
  if (!alert.firing) return "border-[var(--color-border)] bg-white/[0.02]"
  return alert.severity === "page"
    ? "border-red-500/40 bg-red-500/[0.07]"
    : "border-amber-500/40 bg-amber-500/[0.07]"
}

function formatValue(alert: TelephonyAlert): string {
  if (alert.value === null) return "—"
  if (alert.rule.includes("rate") || alert.rule === "telemetry_export") {
    return `${(alert.value * 100).toFixed(1)}%`
  }
  if (alert.rule.endsWith("timeouts") || alert.rule.includes("failures")) return String(alert.value)
  return `${Math.round(alert.value)}ms`
}

/**
 * Every configured rule, firing or not.
 *
 * Quiet rules are shown too: "the rule is quiet" and "the rule was never set
 * up" must not look the same. A rule that cannot meet its minimum sample size
 * says so and does not fire — one bad call out of one never pages anyone.
 */
export function TelephonyAlerts({ alerts }: { alerts: TelephonyAlert[] }) {
  const ordered = [...alerts].sort(
    (a, b) => Number(b.firing) - Number(a.firing) || a.title.localeCompare(b.title),
  )
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {ordered.map((alert) => (
        <div key={alert.rule} className={cn("rounded-lg border px-3 py-2.5 text-xs", severityStyle(alert))}>
          <div className="flex items-center gap-2">
            <span className="font-medium">{alert.title}</span>
            {alert.firing && (
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase",
                  alert.severity === "page" ? "bg-red-500/20 text-red-300" : "bg-amber-500/20 text-amber-300",
                )}
              >
                {alert.severity}
              </span>
            )}
            <span className="ml-auto tabular-nums">
              {formatValue(alert)}
              <span className="text-[var(--color-muted)]">
                {" / "}
                {alert.rule.includes("rate") || alert.rule === "telemetry_export"
                  ? `${(alert.threshold * 100).toFixed(0)}%`
                  : alert.threshold}
              </span>
            </span>
          </div>
          <div className="mt-1 text-[10px] text-[var(--color-muted)]">
            {alert.window_min > 0 && <>window {alert.window_min}m · </>}
            n={alert.samples}
            {alert.min_samples > 0 && <> (min {alert.min_samples})</>}
            {alert.insufficient_data && (
              <span className="ml-1 text-amber-400">insufficient data — cannot fire</span>
            )}
          </div>
          <p className="mt-1 leading-snug text-[var(--color-muted)]">{alert.detail}</p>
          {alert.evidence.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {alert.evidence.map((sid) => (
                <Link
                  key={sid}
                  to={ROUTES.TELEPHONY_CALL.replace(":callRef", sid)}
                  className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10px] hover:bg-white/[0.12]"
                >
                  {sid}
                </Link>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
