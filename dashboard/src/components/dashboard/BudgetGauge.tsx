import { cn } from "@/lib/utils"
import { formatCompactCurrency } from "@/lib/formatters"
import { Skeleton } from "@/components/ui/skeleton"

interface BudgetGaugeProps {
  spent: number
  limit: number
  remaining: number
  loading?: boolean
}

export function BudgetGauge({
  spent,
  limit,
  remaining,
  loading,
}: BudgetGaugeProps) {
  if (loading) {
    return (
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5 flex flex-col items-center">
        <Skeleton className="h-3 w-20 bg-white/[0.06]" />
        <Skeleton className="mt-4 h-24 w-24 rounded-full bg-white/[0.06]" />
      </div>
    )
  }

  const pct = Math.min(spent, 100)
  const radius = 42
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (pct / 100) * circumference

  const color =
    pct > 90
      ? "var(--color-danger)"
      : pct > 70
        ? "var(--color-warning)"
        : "var(--color-accent)"

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
      <p className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider text-center">
        Daily Budget
      </p>
      <div className="flex justify-center mt-4">
        <div className="relative h-28 w-28">
          <svg className="h-full w-full -rotate-90" viewBox="0 0 100 100">
            <circle
              cx="50"
              cy="50"
              r={radius}
              fill="none"
              stroke="rgba(255,255,255,0.06)"
              strokeWidth="6"
            />
            <circle
              cx="50"
              cy="50"
              r={radius}
              fill="none"
              stroke={color}
              strokeWidth="6"
              strokeLinecap="round"
              strokeDasharray={circumference}
              strokeDashoffset={offset}
              className="transition-all duration-700 ease-out"
            />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span
              className={cn("text-lg font-semibold font-mono")}
              style={{ color }}
            >
              {pct.toFixed(0)}%
            </span>
          </div>
        </div>
      </div>
      <div className="mt-3 text-center">
        <p className="text-xs text-[var(--color-muted)]">
          {formatCompactCurrency(remaining)} of{" "}
          {formatCompactCurrency(limit)} remaining
        </p>
      </div>
    </div>
  )
}
