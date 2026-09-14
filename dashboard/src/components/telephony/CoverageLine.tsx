import { AlertTriangle, Database } from "lucide-react"
import type { TelephonyHealth, TelephonyOverview } from "@/api/types"
import { cn } from "@/lib/utils"
import { InfoHint } from "./InfoHint"

/**
 * One line when telemetry is healthy, a warning when it is not.
 *
 * Coverage has to stay on screen — a latency chart drawn over dropped records
 * can show an improvement that is really just missing data — but when nothing
 * is wrong it does not deserve a banner.
 */
export function CoverageLine({
  health,
  coverage,
}: {
  health: TelephonyHealth
  coverage: TelephonyOverview["coverage"]
}) {
  const lost = health.export.dropped_queue_full + health.export.export_failures
  const degraded = !health.enabled || lost > 0 || coverage.telemetry_tables === false
  const sampling = health.sample_rate < 1

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-3 py-1.5 text-[11px]",
        degraded
          ? "border-amber-500/40 bg-amber-500/[0.07] text-amber-200"
          : "border-[var(--color-border)] text-[var(--color-muted)]",
      )}
    >
      {degraded ? <AlertTriangle className="h-3.5 w-3.5" /> : <Database className="h-3.5 w-3.5" />}

      {!health.enabled && <span className="font-medium">Telemetry is disabled — nothing below updates.</span>}
      {coverage.telemetry_tables === false && (
        <span className="font-medium">Telemetry tables missing — run `pincer db upgrade`.</span>
      )}
      {lost > 0 && (
        <span className="font-medium">
          {lost} record(s) lost — charts below are incomplete.
        </span>
      )}

      <span>
        {coverage.calls_with_telemetry}/{coverage.calls} calls traced · {coverage.turns} turns
        {sampling && ` · sampling ${(health.sample_rate * 100).toFixed(0)}%`}
        {coverage.turns_without_response_latency > 0 &&
          ` · ${coverage.turns_without_response_latency} turns without a latency measurement`}
      </span>

      <InfoHint title="Telemetry coverage" className="ml-auto">
        <p>{coverage.note}</p>
        <p>
          Sampling is head-based and per call, so a sampled call is complete. Lifecycle events —
          start, answer, end, failure code — are recorded for every call regardless, which is why the
          call table is never missing rows even at a low sample rate.
        </p>
        <div className="font-mono text-[10px] text-[var(--color-foreground)]">
          <div>queued {health.export.queued}</div>
          <div>exported {health.export.exported}</div>
          <div>dropped {health.export.dropped_queue_full}</div>
          <div>failures {health.export.export_failures}</div>
          <div>
            queue {health.export.queue_depth}/{health.export.queue_capacity}
          </div>
        </div>
        {health.export.last_error && <p className="text-red-400">{health.export.last_error}</p>}
      </InfoHint>
    </div>
  )
}
