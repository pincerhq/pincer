import type { TelephonyFilters } from "@/api/types"

const WINDOWS = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
]

const SELECTS: Array<{ key: keyof TelephonyFilters; label: string; options: string[] }> = [
  { key: "direction", label: "Direction", options: ["inbound", "outbound"] },
  { key: "engine", label: "Engine", options: ["conversation_relay", "media_streams"] },
  { key: "provider", label: "Provider", options: ["twilio"] },
  { key: "status", label: "Status", options: ["active", "connected", "completed", "failed", "ended"] },
  {
    key: "failure_category",
    label: "Failure",
    options: ["none", "technical", "callee_unavailable", "policy_declined", "ended_by_party", "unknown"],
  },
]

const selectClass =
  "rounded-md border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs text-[var(--color-foreground)] [&>option]:bg-[#1a1a1a]"

export function Filters({
  filters,
  onChange,
  environments,
  versions,
  models,
  languages,
}: {
  filters: TelephonyFilters
  onChange: (next: TelephonyFilters) => void
  environments: string[]
  versions: string[]
  models: string[]
  languages: string[]
}) {
  const set = (key: keyof TelephonyFilters, value: string) =>
    onChange({ ...filters, [key]: value || undefined })

  const dynamic: Array<{ key: keyof TelephonyFilters; label: string; options: string[] }> = [
    { key: "environment", label: "Environment", options: environments },
    { key: "app_version", label: "Version", options: versions },
    { key: "model", label: "Model", options: models },
    { key: "language", label: "Language", options: languages },
  ]

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex gap-1">
        {WINDOWS.map((w) => (
          <button
            key={w.hours}
            onClick={() => onChange({ ...filters, hours: w.hours })}
            className={`rounded-md px-2 py-1 text-xs ${
              filters.hours === w.hours
                ? "bg-white/[0.08] text-[var(--color-foreground)]"
                : "text-[var(--color-muted)] hover:bg-white/[0.04]"
            }`}
          >
            {w.label}
          </button>
        ))}
      </div>

      {[...SELECTS, ...dynamic.filter((d) => d.options.length > 0)].map((select) => (
        <select
          key={select.key}
          className={selectClass}
          value={(filters[select.key] as string) ?? ""}
          onChange={(e) => set(select.key, e.target.value)}
        >
          <option value="">{select.label}: any</option>
          {select.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ))}

      <input
        className="min-w-[200px] flex-1 rounded-md border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
        placeholder="Search call SID, trace id or masked number…"
        value={filters.search ?? ""}
        onChange={(e) => set("search", e.target.value)}
      />
    </div>
  )
}
