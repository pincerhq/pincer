import type { CriticalPath } from "@/api/types"
import { formatMs, stageColor, stageLabel } from "./shared"

function isPath(value: CriticalPath | Record<string, never>): value is CriticalPath {
  return Boolean((value as CriticalPath)?.segments?.length)
}

/**
 * The critical path to first response audio, as a partition of the window.
 *
 * The segments here sum EXACTLY to the response latency: overlapping spans were
 * attributed to whichever stage was actually being waited on at each instant,
 * so nothing is double counted and the bar is safe to read as a budget.
 */
export function CriticalPathBar({
  path,
  compact = false,
}: {
  path: CriticalPath | Record<string, never> | undefined
  compact?: boolean
}) {
  if (!path || !isPath(path)) {
    return (
      <p className="text-[11px] text-[var(--color-muted)]">
        No critical path: this turn produced no response audio (cancelled, failed, or handled without
        speaking).
      </p>
    )
  }
  const total = path.total_ms || 1
  return (
    <div className="space-y-1.5">
      <div className="flex h-3 w-full overflow-hidden rounded bg-white/[0.04]">
        {path.segments.map((segment, index) => (
          <div
            key={`${segment.stage}-${index}`}
            style={{
              width: `${(segment.duration_ms / total) * 100}%`,
              backgroundColor: stageColor(segment.stage),
            }}
            title={`${stageLabel(segment.stage)}${segment.label ? ` (${segment.label})` : ""} — ${formatMs(
              segment.duration_ms,
            )}`}
          />
        ))}
      </div>
      {!compact && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px]">
          {path.segments.map((segment, index) => (
            <span key={`${segment.stage}-legend-${index}`} className="flex items-center gap-1">
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ backgroundColor: stageColor(segment.stage) }}
              />
              {stageLabel(segment.stage)}
              {segment.label && <span className="text-[var(--color-muted)]">({segment.label})</span>}
              <span className="tabular-nums text-[var(--color-muted)]">{formatMs(segment.duration_ms)}</span>
            </span>
          ))}
          <span className="text-[var(--color-muted)]">
            total {formatMs(path.total_ms)} · segments sum to the measured response latency
          </span>
        </div>
      )}
    </div>
  )
}
