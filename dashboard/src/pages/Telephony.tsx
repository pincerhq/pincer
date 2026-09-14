import { useMemo, useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { CallCounts } from "@/components/telephony/CallCounts"
import { CallTable } from "@/components/telephony/CallTable"
import { ComparisonTable } from "@/components/telephony/ComparisonTable"
import { Filters } from "@/components/telephony/Filters"
import { DistributionChart, LatencyTrend } from "@/components/telephony/LatencyCharts"
import { Panel } from "@/components/telephony/shared"
import { SlowTurns } from "@/components/telephony/SlowTurns"
import { StageLatency } from "@/components/telephony/StageLatency"
import { TelemetryHealthBanner } from "@/components/telephony/TelemetryHealthBanner"
import { TelephonyAlerts } from "@/components/telephony/TelephonyAlerts"
import { UnavailablePanel } from "@/components/telephony/UnavailablePanel"
import {
  useSlowestTurns,
  useTelephonyAlerts,
  useTelephonyCalls,
  useTelephonyMetricDefinitions,
  useTelephonyOverview,
} from "@/api/hooks/useTelephony"
import { METRIC_LABELS } from "@/components/telephony/shared"
import type { TelephonyFilters } from "@/api/types"

const PAGE_SIZE = 25

/**
 * Telephony overview — the technical view of the phone pipeline.
 *
 * Deliberately not a "health" page: Voice Ops answers whether voice is healthy.
 * This answers where the time goes, which stage is slow, and which call to open
 * next.
 */
export function TelephonyPage() {
  const [filters, setFilters] = useState<TelephonyFilters>({ hours: 24 })
  const [stage, setStage] = useState("response_latency_ms")
  const [page, setPage] = useState({ limit: PAGE_SIZE, offset: 0, sort: "registered_at", order: "desc" })

  const overview = useTelephonyOverview(filters)
  const calls = useTelephonyCalls(filters, page)
  const slowest = useSlowestTurns(filters, 8)
  const alerts = useTelephonyAlerts()
  const definitions = useTelephonyMetricDefinitions()

  const facets = useMemo(() => {
    const rows = calls.data?.calls ?? []
    const unique = (key: keyof (typeof rows)[number]) =>
      [...new Set(rows.map((row) => String(row[key] ?? "")).filter(Boolean))].sort()
    return {
      environments: unique("environment"),
      versions: unique("app_version"),
      models: unique("model"),
      languages: unique("language"),
    }
  }, [calls.data])

  const onSort = (column: string) =>
    setPage((prev) => ({
      ...prev,
      offset: 0,
      sort: column,
      order: prev.sort === column && prev.order === "desc" ? "asc" : "desc",
    }))

  const applyFilters = (next: TelephonyFilters) => {
    setFilters(next)
    setPage((prev) => ({ ...prev, offset: 0 }))
  }

  return (
    <PageContainer title="Telephony">
      <div className="space-y-5">
        <Filters filters={filters} onChange={applyFilters} {...facets} />

        {overview.isLoading || !overview.data ? (
          <Skeleton className="h-24 rounded-xl" />
        ) : (
          <>
            <TelemetryHealthBanner health={overview.data.telemetry} coverage={overview.data.coverage} />
            <CallCounts data={overview.data} />

            <Panel
              title="Alerts"
              subtitle="Thresholds, windows and minimum sample sizes are configurable; a rule below its minimum cannot fire."
            >
              {alerts.isLoading ? (
                <Skeleton className="h-24 rounded-lg" />
              ) : (
                <TelephonyAlerts alerts={alerts.data ?? []} />
              )}
            </Panel>

            <Panel
              title="Latency by pipeline stage"
              subtitle="Select a row to see its distribution. Stages overlap — these durations are not additive."
            >
              <StageLatency
                data={overview.data}
                definitions={definitions.data ?? []}
                onSelect={setStage}
                selected={stage}
              />
            </Panel>

            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title={`Distribution — ${METRIC_LABELS[stage] ?? stage}`}>
                <DistributionChart buckets={overview.data.distributions[stage]} stage={stage} />
              </Panel>
              <Panel
                title="Response latency over time"
                subtitle="Each point is a percentile of that bucket's own turns, never an average of percentiles."
              >
                <LatencyTrend trend={overview.data.trend} />
              </Panel>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Reliability counters" subtitle={`Window: last ${filters.hours}h`}>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
                  {Object.entries(overview.data.reliability).map(([key, value]) => (
                    <div key={key} className="flex items-baseline gap-2">
                      <span className="text-[var(--color-muted)]">{key.replace(/_/g, " ")}</span>
                      <span className="ml-auto tabular-nums">{value}</span>
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel
                title="Call duration & turn counts"
                subtitle="Distribution over the window, not an average."
              >
                <div className="grid grid-cols-2 gap-4 text-xs">
                  <div>
                    <div className="mb-1 text-[var(--color-muted)]">Duration</div>
                    <div>p50 {fmtSeconds(overview.data.calls.duration_summary.p50)}</div>
                    <div>p95 {fmtSeconds(overview.data.calls.duration_summary.p95)}</div>
                    <div className="text-[var(--color-muted)]">
                      n={overview.data.calls.duration_summary.count}
                    </div>
                  </div>
                  <div>
                    <div className="mb-1 text-[var(--color-muted)]">Turns per call</div>
                    <div>p50 {fmtCount(overview.data.calls.turn_count_summary.p50)}</div>
                    <div>p95 {fmtCount(overview.data.calls.turn_count_summary.p95)}</div>
                    <div className="text-[var(--color-muted)]">
                      n={overview.data.calls.turn_count_summary.count}
                    </div>
                  </div>
                </div>
              </Panel>
            </div>

            <Panel
              title="Provider / model comparison"
              subtitle="Response latency per group, each from its own histogram."
            >
              <ComparisonTable comparisons={overview.data.comparisons} />
            </Panel>

            <Panel
              title="Slowest turns"
              subtitle="Measured critical path, not a guess — click through to the call and the supporting spans."
            >
              {slowest.isLoading ? (
                <Skeleton className="h-32 rounded-lg" />
              ) : (
                <SlowTurns turns={slowest.data ?? []} />
              )}
            </Panel>

            <Panel
              title="Not measurable here"
              subtitle="Structural limits of this pipeline, listed so a missing chart is never mistaken for a broken one."
            >
              <UnavailablePanel metrics={overview.data.unavailable} />
            </Panel>
          </>
        )}

        <Panel title="Calls" subtitle="Search, sort, and open any call for its full trace.">
          {calls.isLoading || !calls.data ? (
            <Skeleton className="h-64 rounded-lg" />
          ) : (
            <CallTable
              calls={calls.data.calls}
              total={calls.data.total}
              offset={calls.data.offset}
              limit={calls.data.limit}
              sort={page.sort}
              order={page.order}
              onSort={onSort}
              onPage={(offset) => setPage((prev) => ({ ...prev, offset }))}
            />
          )}
        </Panel>
      </div>
    </PageContainer>
  )
}

function fmtSeconds(value: number | null): string {
  return value === null ? "—" : `${(value / 1000).toFixed(1)}s`
}

function fmtCount(value: number | null): string {
  return value === null ? "—" : value.toFixed(1)
}
