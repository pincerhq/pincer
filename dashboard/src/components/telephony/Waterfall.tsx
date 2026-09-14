import { useMemo } from "react"
import type { TelephonySpan } from "@/api/types"
import { cn } from "@/lib/utils"
import { formatMs, stageColor, stageLabel } from "./shared"

/**
 * Span waterfall for a call or a single turn.
 *
 * Spans overlap on purpose — the pipeline streams, and an LLM span that runs
 * underneath a TTS span is the normal shape of a fast turn, not a bug. Bars are
 * positioned by their monotonic offsets, so overlap is shown truthfully instead
 * of being flattened into a sequence.
 */
export function Waterfall({
  spans,
  onSelect,
  selectedSpanId,
}: {
  spans: TelephonySpan[]
  onSelect?: (span: TelephonySpan) => void
  selectedSpanId?: string
}) {
  const { origin, span: windowMs, rows } = useMemo(() => {
    if (!spans.length) return { origin: 0, span: 1, rows: [] as TelephonySpan[] }
    const starts = spans.map((s) => s.start_offset_ms)
    const ends = spans.map((s) => s.end_offset_ms ?? s.start_offset_ms)
    const min = Math.min(...starts)
    const max = Math.max(...ends)
    return {
      origin: min,
      span: Math.max(max - min, 1),
      rows: [...spans].sort((a, b) => a.start_offset_ms - b.start_offset_ms),
    }
  }, [spans])

  if (!rows.length) {
    return (
      <p className="text-xs text-[var(--color-muted)]">
        No spans recorded. The call may not have been sampled, or it ended before any turn ran.
      </p>
    )
  }

  return (
    <div className="space-y-1">
      {rows.map((span) => {
        const start = ((span.start_offset_ms - origin) / windowMs) * 100
        const end = (((span.end_offset_ms ?? span.start_offset_ms) - origin) / windowMs) * 100
        const width = Math.max(end - start, 0.4)
        const failed = span.status !== "ok"
        return (
          <button
            key={span.span_id}
            onClick={() => onSelect?.(span)}
            className={cn(
              "flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-white/[0.04]",
              selectedSpanId === span.span_id && "bg-white/[0.07]",
            )}
          >
            <span className="w-28 shrink-0 truncate text-[11px]">
              {stageLabel(span.name)}
              {span.attempt > 1 && <span className="text-amber-400"> #{span.attempt}</span>}
            </span>
            <span className="relative h-3 flex-1 rounded bg-white/[0.03]">
              <span
                className={cn("absolute top-0 h-3 rounded", failed && "opacity-70 ring-1 ring-red-400/60")}
                style={{
                  left: `${start}%`,
                  width: `${width}%`,
                  backgroundColor: stageColor(span.name),
                }}
              />
              {span.open && (
                <span className="absolute right-1 top-0 text-[9px] leading-3 text-amber-400">open</span>
              )}
            </span>
            <span className="w-16 shrink-0 text-right text-[11px] tabular-nums text-[var(--color-muted)]">
              {span.duration_ms === null ? "—" : formatMs(span.duration_ms)}
            </span>
            {failed && <span className="w-14 shrink-0 text-[10px] text-red-400">{span.status}</span>}
          </button>
        )
      })}
      <p className="pt-1 text-[10px] text-[var(--color-muted)]">
        Bars overlap where stages ran concurrently. Durations are NOT additive — see the turn's
        critical path for the non-overlapping attribution.
      </p>
    </div>
  )
}
