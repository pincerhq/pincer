import { useState } from "react"
import { AlertTriangle, ChevronDown, ShieldCheck } from "lucide-react"
import { Link } from "react-router-dom"
import type { TelephonyAlert } from "@/api/types"
import { ROUTES } from "@/lib/constants"
import { cn } from "@/lib/utils"
import { InfoHint } from "./InfoHint"

function formatValue(alert: TelephonyAlert): string {
  if (alert.value === null) return "—"
  if (alert.rule.includes("rate") || alert.rule === "telemetry_export") return `${(alert.value * 100).toFixed(1)}%`
  if (alert.rule.endsWith("timeouts") || alert.rule.includes("failures")) return String(alert.value)
  return `${Math.round(alert.value)}ms`
}

function formatThreshold(alert: TelephonyAlert): string {
  if (alert.rule.includes("rate") || alert.rule === "telemetry_export") return `${(alert.threshold * 100).toFixed(0)}%`
  return String(alert.threshold)
}

/**
 * Firing alerts up front; everything quiet folded into one line.
 *
 * A wall of green rules trains people to scroll past the section that is
 * supposed to catch their eye — but hiding quiet rules entirely would make
 * "nothing is wrong" and "no rule exists" look identical, so the count and the
 * full list stay one click away.
 */
export function AlertStrip({ alerts }: { alerts: TelephonyAlert[] }) {
  const [open, setOpen] = useState(false)
  const firing = alerts.filter((alert) => alert.firing)
  const blind = alerts.filter((alert) => !alert.firing && alert.insufficient_data)
  const quiet = alerts.filter((alert) => !alert.firing && !alert.insufficient_data)

  return (
    <div className="space-y-2">
      {firing.map((alert) => (
        <div
          key={alert.rule}
          className={cn(
            "rounded-lg border px-3 py-2",
            alert.severity === "page"
              ? "border-red-500/40 bg-red-500/[0.07]"
              : "border-amber-500/40 bg-amber-500/[0.07]",
          )}
        >
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <AlertTriangle
              className={cn("h-3.5 w-3.5", alert.severity === "page" ? "text-red-400" : "text-amber-400")}
            />
            <span className="font-medium">{alert.title}</span>
            <span className="tabular-nums">
              {formatValue(alert)}
              <span className="text-[var(--color-muted)]"> / {formatThreshold(alert)}</span>
            </span>
            <span className="text-[10px] text-[var(--color-muted)]">
              {alert.window_min > 0 && `${alert.window_min}m · `}n={alert.samples}
            </span>
            <InfoHint title={alert.title}>
              <p>{alert.detail}</p>
              <p className="opacity-70">
                Threshold, window and minimum sample size are configurable; this rule cannot fire below{" "}
                {alert.min_samples} samples.
              </p>
            </InfoHint>
            {alert.evidence.length > 0 && (
              <span className="ml-auto flex flex-wrap gap-1">
                {alert.evidence.slice(0, 4).map((sid) => (
                  <Link
                    key={sid}
                    to={ROUTES.TELEPHONY_CALL.replace(":callRef", sid)}
                    className="rounded bg-white/[0.08] px-1.5 py-0.5 font-mono text-[10px] hover:bg-white/[0.16]"
                  >
                    {sid}
                  </Link>
                ))}
              </span>
            )}
          </div>
        </div>
      ))}

      <button
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-[11px] text-[var(--color-muted)] hover:bg-white/[0.03]"
      >
        {firing.length === 0 && <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />}
        <span>
          {firing.length === 0 ? "No alerts firing" : `${firing.length} firing`} · {quiet.length} quiet
          {blind.length > 0 && ` · ${blind.length} without enough data`}
        </span>
        <ChevronDown className={cn("ml-auto h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="grid gap-1 rounded-lg border border-[var(--color-border)] p-2 md:grid-cols-2">
          {[...quiet, ...blind].map((alert) => (
            <div key={alert.rule} className="flex items-center gap-2 px-1.5 py-1 text-[11px]">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  alert.insufficient_data ? "bg-[var(--color-muted)]" : "bg-emerald-500",
                )}
              />
              <span className="truncate">{alert.title}</span>
              <span className="ml-auto tabular-nums text-[var(--color-muted)]">
                {alert.insufficient_data ? `n=${alert.samples}/${alert.min_samples}` : formatValue(alert)}
              </span>
              <InfoHint title={alert.title}>
                <p>{alert.detail}</p>
                {alert.insufficient_data && (
                  <p className="text-amber-400/90">
                    Not enough data to evaluate ({alert.reason || "below the minimum sample size"}), so
                    this rule cannot fire. Quiet is not the same as clear.
                  </p>
                )}
              </InfoHint>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
