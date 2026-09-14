import { AlertTriangle, CheckCircle2, Info } from "lucide-react"
import type { TelephonyHealth, TelephonyOverview } from "@/api/types"
import { cn } from "@/lib/utils"

/**
 * Coverage first, numbers second.
 *
 * Every chart below this banner is computed over whatever telemetry actually
 * reached storage. If records were dropped or the call was sampled out, the
 * charts are still drawn — but an engineer must not read them as complete, and
 * a *drop* in latency while export is failing is not an improvement.
 */
export function TelemetryHealthBanner({
  health,
  coverage,
}: {
  health: TelephonyHealth
  coverage: TelephonyOverview["coverage"]
}) {
  const lost = health.export.dropped_queue_full + health.export.export_failures
  const sampling = health.sample_rate < 1
  const degraded = !health.enabled || lost > 0 || coverage.telemetry_tables === false

  const tone = degraded
    ? "border-amber-500/30 bg-amber-500/[0.06] text-amber-200"
    : "border-[var(--color-border)] bg-white/[0.02] text-[var(--color-muted)]"

  const Icon = degraded ? AlertTriangle : sampling ? Info : CheckCircle2

  return (
    <div className={cn("rounded-xl border px-4 py-3 text-xs", tone)}>
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="space-y-1">
          {!health.enabled && (
            <p className="font-medium">
              Telephony telemetry is disabled — set PINCER_TELEPHONY_TELEMETRY_ENABLED=true. Nothing
              below is being updated.
            </p>
          )}
          {coverage.telemetry_tables === false && (
            <p className="font-medium">
              Telemetry tables are missing. Run <code>pincer db upgrade</code>.
            </p>
          )}
          {lost > 0 && (
            <p className="font-medium">
              {lost} telemetry record(s) lost ({health.export.dropped_queue_full} dropped on a full
              queue, {health.export.export_failures} failed to export). Charts below are incomplete;
              an apparent improvement may just be missing data.
              {health.export.last_error && <> Last error: {health.export.last_error}</>}
            </p>
          )}
          <p>
            {coverage.calls_with_telemetry}/{coverage.calls} calls traced
            {sampling && <> · sampling at {(health.sample_rate * 100).toFixed(0)}%</>}
            {coverage.calls_partial > 0 && <> · {coverage.calls_partial} with partial coverage</>}
            {" · "}
            {coverage.turns} turns
            {coverage.turns_without_response_latency > 0 && (
              <> ({coverage.turns_without_response_latency} without a response-latency measurement)</>
            )}
            {" · queue "}
            {health.export.queue_depth}/{health.export.queue_capacity}
          </p>
          {sampling && (
            <p className="text-[11px] opacity-80">
              Sampling is per call and head-based, so a sampled call is complete. Lifecycle events
              (start / answer / end / failure code) are recorded for every call regardless, which is
              why the call table is never missing rows.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
