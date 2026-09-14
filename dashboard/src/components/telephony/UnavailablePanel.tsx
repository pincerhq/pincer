import type { TelephonyMetricDefinition } from "@/api/types"
import { SourceBadge } from "./shared"

/**
 * Metrics this deployment structurally cannot produce.
 *
 * Listed explicitly so a missing chart reads as a known limit of the pipeline
 * rather than a broken dashboard — and so nobody quietly substitutes a number
 * that means something else (audio SENT is not audio HEARD).
 */
export function UnavailablePanel({
  metrics,
}: {
  metrics: Array<TelephonyMetricDefinition & { engines?: string[] }>
}) {
  if (!metrics.length) {
    return (
      <p className="text-xs text-[var(--color-muted)]">
        Every defined metric is observable for the engines seen in this window.
      </p>
    )
  }
  return (
    <ul className="space-y-2.5">
      {metrics.map((metric) => (
        <li key={metric.key} className="text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{metric.label}</span>
            <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-[var(--color-muted)]">
              Unavailable
            </span>
            <SourceBadge source={metric.source} />
            {metric.engines && metric.engines.length > 0 && (
              <span className="font-mono text-[10px] text-[var(--color-muted)]">
                {metric.engines.join(", ")}
              </span>
            )}
          </div>
          <p className="mt-0.5 max-w-[80ch] leading-snug text-[var(--color-muted)]">
            {metric.unavailable_reason}
          </p>
        </li>
      ))}
    </ul>
  )
}
