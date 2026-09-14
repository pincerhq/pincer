import { Skeleton } from "@/components/ui/skeleton"
import { AlertStrip } from "@/components/telephony/AlertStrip"
import { Block } from "@/components/telephony/shared"
import { useTelephonyAlerts } from "@/api/hooks/useTelephony"

/** Every rule, its current value, and why it is or is not firing. */
export function TelephonyAlertsPage() {
  const alerts = useTelephonyAlerts()

  if (alerts.isLoading) return <Skeleton className="h-64 rounded-xl" />

  const rows = alerts.data ?? []

  return (
    <div className="space-y-4">
      <AlertStrip alerts={rows} />

      <Block
        title="Rules"
        info={
          <>
            <p>
              Thresholds, evaluation windows and minimum sample sizes are configurable per rule
              (<span className="font-mono">PINCER_ALERT_*</span>). A rule below its minimum sample
              size reports "insufficient data" and <strong>cannot fire</strong> — one bad call out of
              one must never page anyone.
            </p>
            <p>
              Set thresholds from observed baselines rather than the shipped defaults:{" "}
              <span className="font-mono">pincer telephony baseline</span> prints your own
              percentiles and suggests values.
            </p>
          </>
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-[var(--color-border)] text-left text-[11px] text-[var(--color-muted)]">
                <th className="py-2 pr-3 font-medium">Rule</th>
                <th className="py-2 pr-3 font-medium">State</th>
                <th className="py-2 pr-3 text-right font-medium">Value</th>
                <th className="py-2 pr-3 text-right font-medium">Threshold</th>
                <th className="py-2 pr-3 text-right font-medium">Window</th>
                <th className="py-2 pr-3 text-right font-medium">Samples</th>
                <th className="py-2 font-medium">Severity</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((alert) => (
                <tr key={alert.rule} className="border-b border-[var(--color-border)]/60 align-top">
                  <td className="py-2 pr-3">
                    <div className="font-medium">{alert.title}</div>
                    <div className="font-mono text-[10px] text-[var(--color-muted)]">{alert.rule}</div>
                    <p className="mt-0.5 max-w-[60ch] text-[10px] leading-snug text-[var(--color-muted)]">
                      {alert.detail}
                    </p>
                  </td>
                  <td className="py-2 pr-3 text-xs">
                    {alert.firing ? (
                      <span className="text-red-400">firing</span>
                    ) : alert.insufficient_data ? (
                      <span className="text-[var(--color-muted)]" title={alert.reason}>
                        no data
                      </span>
                    ) : (
                      <span className="text-emerald-400">ok</span>
                    )}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatValue(alert.rule, alert.value)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums text-[var(--color-muted)]">
                    {formatValue(alert.rule, alert.threshold)}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums text-[var(--color-muted)]">
                    {alert.window_min ? `${alert.window_min}m` : "—"}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    <span className={alert.insufficient_data ? "text-amber-400" : ""}>
                      {alert.samples}
                      {alert.min_samples > 0 && <span className="opacity-50">/{alert.min_samples}</span>}
                    </span>
                  </td>
                  <td className="py-2 text-[10px] uppercase text-[var(--color-muted)]">{alert.severity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Block>
    </div>
  )
}

function formatValue(rule: string, value: number | null): string {
  if (value === null) return "—"
  if (rule.includes("rate") || rule === "telemetry_export") return `${(value * 100).toFixed(1)}%`
  if (rule.endsWith("timeouts") || rule.includes("failures")) return String(value)
  return `${Math.round(value)}ms`
}
