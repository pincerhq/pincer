import type { TelephonyOverview } from "@/api/types"
import { formatRate } from "./shared"

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-white/[0.02] px-3 py-2.5" title={hint}>
      <div className="text-[11px] text-[var(--color-muted)]">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  )
}

/** Counts and rates, each rate captioned with the denominator it was divided by. */
export function CallCounts({ data }: { data: TelephonyOverview }) {
  const c = data.calls
  const r = data.rates
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Active" value={String(c.active)} hint="Calls currently in progress" />
        <Stat
          label="Attempted"
          value={String(c.attempted)}
          hint="Excludes calls declined by policy before dialling"
        />
        <Stat label="Connected" value={String(c.connected)} hint="Reached the answered state" />
        <Stat label="Completed" value={String(c.completed)} />
        <Stat
          label="Technical failures"
          value={String(c.technical_failures)}
          hint="Failures that are ours — excludes busy / no-answer / policy declines"
        />
        <Stat
          label="Declined by policy"
          value={String(c.declined_by_policy)}
          hint="Blocklist, capacity, quiet hours, do-not-call. The system working, not failing."
        />
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        {[
          ["connection_rate", "Connection rate"],
          ["technical_failure_rate", "Technical failure rate"],
          ["unexpected_disconnect_rate", "Unexpected disconnect rate"],
        ].map(([key, label]) => {
          const rate = r[key]
          if (!rate) return null
          return (
            <div
              key={key}
              className="rounded-lg border border-[var(--color-border)] bg-white/[0.02] px-3 py-2.5"
            >
              <div className="flex items-baseline gap-2">
                <span className="text-[11px] text-[var(--color-muted)]">{label}</span>
                <span className="ml-auto text-base font-semibold tabular-nums">
                  {formatRate(rate.value)}
                </span>
              </div>
              <div className="mt-1 text-[11px] text-[var(--color-muted)]">
                {rate.numerator} / {rate.denominator}
              </div>
              <p className="mt-1.5 text-[10px] leading-snug text-[var(--color-muted)]">
                {data.denominators[key]}
              </p>
            </div>
          )
        })}
      </div>
    </div>
  )
}
