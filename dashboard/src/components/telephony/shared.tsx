import type { LatencySummary } from "@/api/types"
import { cn } from "@/lib/utils"
import { InfoHint } from "./InfoHint"

/**
 * A measured number and a missing one must never look alike.
 *
 * `null` means "not measured" — the stage did not run, or this engine cannot
 * observe it. Rendering that as `0 ms` would make the least observable pipeline
 * look like the fastest one, so it renders as an em dash with a tooltip.
 */
export function Ms({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value === null || value === undefined) {
    return (
      <span className={cn("text-[var(--color-muted)]", className)} title="Not measured">
        —
      </span>
    )
  }
  return <span className={className}>{formatMs(value)}</span>
}

/**
 * "Based on the N most recent of M calls."
 *
 * Anything counted in the browser from the call list is counted from at most
 * 500 rows — the API caps `limit` at 500 (`le=500`) — so the moment a filter
 * matches more than that, a chart drawn from it describes a sample of the
 * window rather than the window. The percentages stay real; what changes is
 * what they are percentages OF, and that has to be on screen or the reader has
 * no way to know. Amber because it is the same "treat this with caution"
 * signal the under-sampled percentile labels already use.
 *
 * Renders nothing when the list is complete, so it costs nothing on the small
 * tenants where it does not apply.
 *
 * Distinct from `SampleNote` below, which asks whether a percentile has enough
 * observations to mean anything. This one asks whether we loaded all the rows
 * at all — a count can be perfectly well-sampled and still be of the wrong
 * population.
 */
export function TruncationNote({ loaded, total }: { loaded: number; total: number }) {
  if (!total || total <= loaded) return null
  return (
    <p className="mt-2 text-[11px] text-amber-400">
      Based on the {loaded.toLocaleString()} most recent of {total.toLocaleString()} matching calls —
      the call list is capped, so this is a sample of the window, not all of it.
    </p>
  )
}

export function formatMs(value: number): string {
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}s`
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`
  if (value >= 100) return `${Math.round(value)}ms`
  return `${value.toFixed(1)}ms`
}

export function formatRate(value: number | null): string {
  if (value === null) return "—"
  return `${(value * 100).toFixed(1)}%`
}

/** Human labels for the stage/span vocabulary. */
export const STAGE_LABELS: Record<string, string> = {
  "stt.utterance": "STT",
  turn: "Turn",
  "agent.prep": "Agent prep",
  "llm.generation": "LLM",
  "tool.execution": "Tool",
  "tool.approval": "Approval hold",
  "tts.synthesis": "TTS",
  "audio.outbound": "Audio out",
  "call.setup": "Call setup",
  "call.media": "Media",
  call: "Call",
  unattributed: "Unattributed",
}

export const STAGE_COLORS: Record<string, string> = {
  "stt.utterance": "#38bdf8",
  "agent.prep": "#a78bfa",
  "llm.generation": "#6366f1",
  "tool.execution": "#f59e0b",
  "tool.approval": "#f472b6",
  "tts.synthesis": "#10b981",
  "audio.outbound": "#22d3ee",
  turn: "#64748b",
  unattributed: "#475569",
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

export function stageColor(stage: string): string {
  return STAGE_COLORS[stage] ?? "#64748b"
}

export const METRIC_LABELS: Record<string, string> = {
  response_latency_ms: "Response latency (sent)",
  endpointing_ms: "Endpointing delay",
  stt_first_partial_ms: "STT first partial",
  stt_final_ms: "STT finalisation",
  agent_queue_ms: "Agent queueing",
  agent_prep_ms: "Agent preparation",
  llm_ttft_ms: "LLM time to first token",
  llm_total_ms: "LLM generation",
  tool_total_ms: "Tool execution",
  tts_first_audio_ms: "TTS time to first audio",
  tts_total_ms: "TTS synthesis",
  audio_queue_ms: "Audio queue residence",
  total_ms: "Turn total (incl. tail)",
}

/**
 * Badge for where a number came from. "Sent" and "heard" are different claims
 * and the UI has to keep saying so — this is the whole point of the badge.
 */
export function SourceBadge({ source }: { source: string }) {
  const styles: Record<string, string> = {
    server_measured: "bg-emerald-500/15 text-emerald-400",
    provider_reported: "bg-sky-500/15 text-sky-400",
    estimated: "bg-amber-500/15 text-amber-400",
    unavailable: "bg-white/[0.06] text-[var(--color-muted)]",
  }
  const labels: Record<string, string> = {
    server_measured: "server-measured",
    provider_reported: "provider-reported",
    estimated: "estimated",
    unavailable: "unavailable",
  }
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", styles[source] ?? styles.unavailable)}>
      {labels[source] ?? source}
    </span>
  )
}

/** p99 over nine samples is noise; say so next to the number. */
export function SampleNote({ summary }: { summary: LatencySummary }) {
  if (summary.count === 0) {
    return <span className="text-[11px] text-[var(--color-muted)]">no samples</span>
  }
  if (!summary.sufficient_samples) {
    return (
      <span
        className="text-[11px] text-amber-400"
        title={`Fewer than ${summary.min_samples} samples — percentiles here are indicative only.`}
      >
        n={summary.count} (low)
      </span>
    )
  }
  return <span className="text-[11px] text-[var(--color-muted)]">n={summary.count}</span>
}

export function Block({
  title,
  info,
  actions,
  children,
  className,
  dense = false,
}: {
  title: string
  /** The explanation, folded behind an "i". Never omitted, never shouted. */
  info?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  className?: string
  dense?: boolean
}) {
  return (
    <section
      className={cn(
        "rounded-xl border border-[var(--color-border)] bg-white/[0.02]",
        dense ? "p-3" : "p-4",
        className,
      )}
    >
      <div className="mb-3 flex items-center gap-1.5">
        <h2 className="text-[13px] font-medium">{title}</h2>
        {info && <InfoHint title={title}>{info}</InfoHint>}
        {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
      </div>
      {children}
    </section>
  )
}

/** A clickable series/segment legend entry. */
export function LegendDot({ color }: { color: string }) {
  return <span className="inline-block h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: color }} />
}
