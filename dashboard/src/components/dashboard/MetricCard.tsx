import { cn } from "@/lib/utils"
import { Skeleton } from "@/components/ui/skeleton"

interface MetricCardProps {
  label: string
  value: string | number
  subtext?: string
  trend?: number
  variant?: "default" | "danger"
  loading?: boolean
}

export function MetricCard({
  label,
  value,
  subtext,
  trend,
  variant = "default",
  loading,
}: MetricCardProps) {
  if (loading) {
    return (
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
        <Skeleton className="h-3 w-20 bg-white/[0.06]" />
        <Skeleton className="mt-3 h-8 w-24 bg-white/[0.06]" />
        <Skeleton className="mt-2 h-3 w-16 bg-white/[0.06]" />
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5 hover:border-[var(--color-border-hover)] transition-colors">
      <p className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider">
        {label}
      </p>
      <p
        className={cn(
          "text-3xl font-semibold tracking-tight mt-2",
          variant === "danger" &&
            Number(value) > 0 &&
            "text-[var(--color-danger)]",
        )}
      >
        {value}
      </p>
      <div className="flex items-center gap-2 mt-1">
        {subtext && (
          <span className="text-xs text-[var(--color-muted)]">{subtext}</span>
        )}
        {trend !== undefined && (
          <span
            className={cn(
              "text-xs font-mono",
              trend > 80
                ? "text-[var(--color-warning)]"
                : trend > 50
                  ? "text-[var(--color-muted)]"
                  : "text-[var(--color-success)]",
            )}
          >
            {trend.toFixed(0)}% used
          </span>
        )}
      </div>
    </div>
  )
}
