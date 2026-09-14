import { Skeleton } from "@/components/ui/skeleton"
import { CoverageLine } from "@/components/telephony/CoverageLine"
import { FailureCodes } from "@/components/telephony/FailureCodes"
import { OutcomeMix } from "@/components/telephony/OutcomeMix"
import { ReliabilityTiles } from "@/components/telephony/ReliabilityTiles"
import { Block } from "@/components/telephony/shared"
import { useTelephonyCalls, useTelephonyOverview } from "@/api/hooks/useTelephony"
import { useTelephonyContext } from "./context"

/** How calls ended, what went wrong on the way, and how complete the record is. */
export function TelephonyReliability() {
  const { filters, setFilters, query } = useTelephonyContext()
  const overview = useTelephonyOverview(filters)
  const calls = useTelephonyCalls(filters, { limit: 500, offset: 0, sort: "registered_at", order: "desc" })

  if (overview.isLoading || !overview.data) {
    return <Skeleton className="h-96 rounded-xl" />
  }

  const coverage = overview.data.coverage

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <OutcomeMix
          calls={calls.data?.calls ?? []}
          filters={filters}
          onChange={setFilters}
          query={query}
        />
        <Block
          title="Telemetry coverage"
          info={
            <>
              <p>{coverage.note}</p>
              <p>
                Aggregates elsewhere on this page are only as good as this. A latency chart drawn
                over dropped records can show an improvement that is really just missing data.
              </p>
            </>
          }
        >
          <div className="space-y-3">
            <CoverageLine health={overview.data.telemetry} coverage={coverage} />
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px] sm:grid-cols-3">
              {[
                ["Calls traced", `${coverage.calls_with_telemetry}/${coverage.calls}`],
                ["Partial coverage", String(coverage.calls_partial)],
                ["Turns", String(coverage.turns)],
                ["Turns without latency", String(coverage.turns_without_response_latency)],
                ["Incomplete turns", String(coverage.turns_incomplete)],
                ["Sample rates seen", coverage.sample_rates_seen.map((r) => `${(r * 100).toFixed(0)}%`).join(", ")],
              ].map(([label, value]) => (
                <div key={label} className="flex items-baseline gap-2">
                  <dt className="text-[var(--color-muted)]">{label}</dt>
                  <dd className="ml-auto tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </div>
        </Block>
      </div>

      <ReliabilityTiles data={overview.data} />

      <FailureCodes
        calls={calls.data?.calls ?? []}
        filters={filters}
        onChange={setFilters}
        query={query}
      />
    </div>
  )
}
