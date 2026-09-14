import { X } from "lucide-react"
import type { TelephonyFilters } from "@/api/types"

const LABELS: Partial<Record<keyof TelephonyFilters, string>> = {
  direction: "direction",
  engine: "engine",
  provider: "provider",
  model: "model",
  language: "language",
  status: "status",
  failure_category: "failure",
  environment: "env",
  app_version: "version",
  search: "search",
}

/**
 * The filters currently applied, each removable.
 *
 * Clicking a chart to drill in is only safe if the resulting filter is visible
 * and reversible — otherwise people end up reading a filtered number as the
 * global one.
 */
export function ActiveFilters({
  filters,
  onChange,
}: {
  filters: TelephonyFilters
  onChange: (next: TelephonyFilters) => void
}) {
  const active = (Object.keys(LABELS) as Array<keyof TelephonyFilters>)
    .filter((key) => Boolean(filters[key]))
    .map((key) => ({ key, value: String(filters[key]) }))

  if (!active.length) return null

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {active.map(({ key, value }) => (
        <button
          key={key}
          onClick={() => onChange({ ...filters, [key]: undefined })}
          className="group inline-flex items-center gap-1 rounded-full border border-[var(--color-accent)]/30 bg-[var(--color-accent)]/10 px-2 py-0.5 text-[10px] text-[var(--color-foreground)] transition-colors hover:border-[var(--color-accent)]/60"
        >
          <span className="opacity-60">{LABELS[key]}</span>
          <span className="font-medium">{value}</span>
          <X className="h-2.5 w-2.5 opacity-50 group-hover:opacity-100" />
        </button>
      ))}
      <button
        onClick={() => onChange({ hours: filters.hours })}
        className="rounded-full px-2 py-0.5 text-[10px] text-[var(--color-muted)] hover:text-[var(--color-foreground)]"
      >
        clear all
      </button>
    </div>
  )
}
