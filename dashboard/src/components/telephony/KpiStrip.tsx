import type { TelephonyFilters, TelephonyOverview } from "@/api/types"
import { cn } from "@/lib/utils"
import { InfoHint } from "./InfoHint"
import { formatRate } from "./shared"

type Filters = TelephonyFilters

interface Kpi {
  key: string
  label: string
  value: string
  sub?: string
  info: React.ReactNode
  tone?: "neutral" | "good" | "warn" | "bad"
  filter?: Partial<Filters>
}

const TONES: Record<string, string> = {
  neutral: "text-[var(--color-foreground)]",
  good: "text-emerald-400",
  warn: "text-amber-400",
  bad: "text-red-400",
}

/**
 * The six numbers worth a glance, each one a filter.
 *
 * Clicking a tile drills the whole page into that slice — which is the point of
 * a KPI strip: it should be the way in, not a read-only header.
 */
export function KpiStrip({
  data,
  filters,
  onChange,
}: {
  data: TelephonyOverview
  filters: Filters
  onChange: (next: Filters) => void
}) {
  const c = data.calls
  const r = data.rates
  const d = data.denominators

  const kpis: Kpi[] = [
    {
      key: "active",
      label: "Active now",
      value: String(c.active),
      info: <p>Calls in progress. Their totals are provisional until they end.</p>,
      tone: c.active > 0 ? "good" : "neutral",
      filter: { status: "active" },
    },
    {
      key: "attempted",
      label: "Attempted",
      value: String(c.attempted),
      sub: c.declined_by_policy ? `+${c.declined_by_policy} declined` : undefined,
      info: (
        <p>
          Calls that were actually attempted. Excludes {c.declined_by_policy} declined by policy
          (blocklist, capacity, quiet hours, do-not-call) — those were never attempts, and counting
          them would make a working guardrail look like a failure.
        </p>
      ),
    },
    {
      key: "connection",
      label: "Connection rate",
      value: formatRate(r.connection_rate?.value ?? null),
      sub: `${r.connection_rate?.numerator ?? 0}/${r.connection_rate?.denominator ?? 0}`,
      info: <p>{d.connection_rate}</p>,
      tone:
        r.connection_rate?.value === null || r.connection_rate?.value === undefined
          ? "neutral"
          : r.connection_rate.value >= 0.9
            ? "good"
            : "bad",
    },
    {
      key: "technical",
      label: "Technical failures",
      value: formatRate(r.technical_failure_rate?.value ?? null),
      sub: `${c.technical_failures} call${c.technical_failures === 1 ? "" : "s"}`,
      info: <p>{d.technical_failure_rate}</p>,
      tone:
        r.technical_failure_rate?.value === null || r.technical_failure_rate?.value === undefined
          ? "neutral"
          : r.technical_failure_rate.value > 0.05
            ? "bad"
            : "good",
      filter: { failure_category: "technical" },
    },
    {
      key: "disconnect",
      label: "Unexpected drops",
      value: formatRate(r.unexpected_disconnect_rate?.value ?? null),
      sub: `${r.unexpected_disconnect_rate?.numerator ?? 0}/${r.unexpected_disconnect_rate?.denominator ?? 0}`,
      info: <p>{d.unexpected_disconnect_rate}</p>,
      tone:
        r.unexpected_disconnect_rate?.value === null || r.unexpected_disconnect_rate?.value === undefined
          ? "neutral"
          : r.unexpected_disconnect_rate.value > 0.02
            ? "bad"
            : "good",
    },
    {
      key: "turns",
      label: "Turns",
      value: String(data.coverage.turns),
      sub: `${c.turn_count_summary.p50?.toFixed(1) ?? "—"} median/call`,
      info: (
        <p>
          Conversation turns measured in this window. The per-stage percentiles below are computed
          over these, so this is the sample size behind every latency number on the page.
        </p>
      ),
    },
  ]

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {kpis.map((kpi) => {
        const isActive =
          kpi.filter &&
          Object.entries(kpi.filter).every(([key, value]) => filters[key as keyof Filters] === value)
        const clickable = Boolean(kpi.filter)
        return (
          <div
            key={kpi.key}
            role={clickable ? "button" : undefined}
            tabIndex={clickable ? 0 : undefined}
            onClick={() => kpi.filter && onChange({ ...filters, ...(isActive ? resetOf(kpi.filter) : kpi.filter) })}
            onKeyDown={(e) => {
              if (clickable && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault()
                onChange({ ...filters, ...(isActive ? resetOf(kpi.filter!) : kpi.filter!) })
              }
            }}
            className={cn(
              "rounded-lg border px-3 py-2.5 transition-colors",
              isActive
                ? "border-[var(--color-accent)]/50 bg-[var(--color-accent)]/[0.08]"
                : "border-[var(--color-border)] bg-white/[0.02]",
              clickable && "cursor-pointer hover:border-white/20",
            )}
          >
            <div className="flex items-center gap-1">
              <span className="truncate text-[10px] uppercase tracking-wide text-[var(--color-muted)]">
                {kpi.label}
              </span>
              <InfoHint title={kpi.label} className="ml-auto">
                {kpi.info}
              </InfoHint>
            </div>
            <div className={cn("mt-1 text-xl font-semibold tabular-nums", TONES[kpi.tone ?? "neutral"])}>
              {kpi.value}
            </div>
            <div className="h-3 text-[10px] text-[var(--color-muted)]">{kpi.sub ?? ""}</div>
          </div>
        )
      })}
    </div>
  )
}

function resetOf(filter: Partial<Filters>): Partial<Filters> {
  return Object.fromEntries(Object.keys(filter).map((key) => [key, undefined])) as Partial<Filters>
}
