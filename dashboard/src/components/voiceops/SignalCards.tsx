import type { GoldenSignal } from "@/api/types"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const SIGNAL_LABELS: Record<string, string> = {
  call_success_rate: "Call success rate",
  booking_success_rate: "Booking success rate",
  turn_latency_p95: "Turn latency p95",
  stuck_calls: "Stuck calls",
  cost_per_call: "Cost per call",
}

/** Lower values are bad for these; higher values are bad for the rest. */
const LOWER_IS_BAD = new Set(["call_success_rate", "booking_success_rate"])

function formatSignal(signal: GoldenSignal): string {
  if (!signal.sufficient_data || signal.value == null) return "—"
  switch (signal.unit) {
    case "ratio":
      return `${(signal.value * 100).toFixed(0)}%`
    case "count":
      return String(Math.round(signal.value))
    case "ratio_to_baseline":
      return `${signal.value.toFixed(1)}×`
    case "s":
      return `${signal.value.toFixed(2)}s`
    default:
      return signal.value.toFixed(2)
  }
}

function signalBreached(signal: GoldenSignal): boolean {
  if (!signal.sufficient_data || signal.value == null || signal.target == null) return false
  return LOWER_IS_BAD.has(signal.name)
    ? signal.value < signal.target
    : signal.value > signal.target
}

function thresholdLabel(signal: GoldenSignal): string {
  if (signal.target == null) return ""
  switch (signal.unit) {
    case "ratio":
      return `min ${(signal.target * 100).toFixed(0)}%`
    case "ratio_to_baseline":
      return `max ${signal.target}× baseline`
    case "s":
      return `max ${signal.target}s`
    default:
      return `max ${signal.target}`
  }
}

function SignalCard({ signal }: { signal: GoldenSignal }) {
  const breached = signalBreached(signal)
  const noData = !signal.sufficient_data

  return (
    <div
      className={cn(
        "rounded-xl border bg-[var(--color-card)] p-4",
        breached
          ? "border-red-500/40 bg-red-500/5"
          : noData
            ? "border-[var(--color-border)]"
            : "border-emerald-500/25",
      )}
    >
      <div className="text-xs text-[var(--color-muted)]">
        {SIGNAL_LABELS[signal.name] ?? signal.name}
      </div>
      <div
        className={cn(
          "mt-1 text-2xl font-semibold tabular-nums",
          breached ? "text-red-400" : noData ? "text-[var(--color-muted)]" : "text-emerald-400",
        )}
      >
        {formatSignal(signal)}
      </div>
      <div className="mt-1 text-[11px] text-[var(--color-muted)]">
        {noData ? (
          /* Never render "0%" for an unobserved system — a quiet install is
             not a broken one, and showing green would be just as misleading. */
          <span>
            not enough data ({signal.sample_size}/{signal.min_sample})
          </span>
        ) : (
          <span>
            {thresholdLabel(signal)} · {signal.sample_size} over {signal.window}
          </span>
        )}
      </div>
    </div>
  )
}

interface SignalCardsProps {
  signals?: Record<string, GoldenSignal>
  loading?: boolean
}

const ORDER = [
  "call_success_rate",
  "booking_success_rate",
  "turn_latency_p95",
  "stuck_calls",
  "cost_per_call",
]

export function SignalCards({ signals, loading }: SignalCardsProps) {
  if (loading || !signals) {
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {ORDER.map((name) => (
          <Skeleton key={name} className="h-[104px] rounded-xl" />
        ))}
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
      {ORDER.filter((name) => signals[name]).map((name) => (
        <SignalCard key={name} signal={signals[name]} />
      ))}
    </div>
  )
}
