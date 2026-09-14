import { useMemo, useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { ActiveFilters } from "@/components/telephony/ActiveFilters"
import { AlertStrip } from "@/components/telephony/AlertStrip"
import { CallTable } from "@/components/telephony/CallTable"
import { ComparisonChart } from "@/components/telephony/ComparisonChart"
import { CoverageLine } from "@/components/telephony/CoverageLine"
import { ExportMenu } from "@/components/telephony/ExportMenu"
import { Filters } from "@/components/telephony/Filters"
import { KpiStrip } from "@/components/telephony/KpiStrip"
import { DistributionChart } from "@/components/telephony/LatencyCharts"
import { LatencyHero } from "@/components/telephony/LatencyHero"
import { OutcomeMix } from "@/components/telephony/OutcomeMix"
import { ReliabilityTiles } from "@/components/telephony/ReliabilityTiles"
import { SlowTurns } from "@/components/telephony/SlowTurns"
import { StageBars } from "@/components/telephony/StageBars"
import { Block, METRIC_LABELS } from "@/components/telephony/shared"
import {
  filterParams,
  useSlowestTurns,
  useTelephonyAlerts,
  useTelephonyCalls,
  useTelephonyMetricDefinitions,
  useTelephonyOverview,
} from "@/api/hooks/useTelephony"
import type { TelephonyFilters } from "@/api/types"

const PAGE_SIZE = 25

/**
 * Telephony — the technical view of the phone pipeline.
 *
 * Built as drill-in blocks rather than a report: the KPI tiles, the outcome
 * donut and the comparison bars are all filters, applied filters show as
 * removable chips, and every definition, denominator and caveat lives behind an
 * "i" so the numbers stay readable. Voice Ops answers "is voice healthy"; this
 * answers "where does the time go, and which call do I open next".
 */
export function TelephonyPage() {
  const [filters, setFilters] = useState<TelephonyFilters>({ hours: 24 })
  const [stage, setStage] = useState("response_latency_ms")
  const [page, setPage] = useState({ limit: PAGE_SIZE, offset: 0, sort: "registered_at", order: "desc" })

  const overview = useTelephonyOverview(filters)
  const calls = useTelephonyCalls(filters, page)
  const slowest = useSlowestTurns(filters, 6)
  const alerts = useTelephonyAlerts()
  const definitions = useTelephonyMetricDefinitions()

  const query = useMemo(() => filterParams(filters), [filters])

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

  const applyFilters = (next: TelephonyFilters) => {
    setFilters(next)
    setPage((prev) => ({ ...prev, offset: 0 }))
  }

  const onSort = (column: string) =>
    setPage((prev) => ({
      ...prev,
      offset: 0,
      sort: column,
      order: prev.sort === column && prev.order === "desc" ? "asc" : "desc",
    }))

  const definition = definitions.data?.find((d) => d.key === stage)

  return (
    <PageContainer title="Telephony">
      <div className="space-y-4">
        <div className="space-y-2">
          <Filters filters={filters} onChange={applyFilters} {...facets} />
          <ActiveFilters filters={filters} onChange={applyFilters} />
        </div>

        {overview.isLoading || !overview.data ? (
          <>
            <Skeleton className="h-20 rounded-xl" />
            <Skeleton className="h-64 rounded-xl" />
          </>
        ) : (
          <>
            <CoverageLine health={overview.data.telemetry} coverage={overview.data.coverage} />

            {alerts.isLoading ? (
              <Skeleton className="h-9 rounded-lg" />
            ) : (
              <AlertStrip alerts={alerts.data ?? []} />
            )}

            <KpiStrip data={overview.data} filters={filters} onChange={applyFilters} />

            <LatencyHero
              data={overview.data}
              definition={definitions.data?.find((d) => d.key === "response_latency_ms")}
              query={query}
            />

            <div className="grid gap-4 xl:grid-cols-2">
              <StageBars
                data={overview.data}
                definitions={definitions.data ?? []}
                selected={stage}
                onSelect={setStage}
                query={query}
              />
              <Block
                title={`Distribution · ${METRIC_LABELS[stage] ?? stage}`}
                info={
                  <>
                    <p>
                      How that stage's measurements are spread, not just their percentiles. A long
                      right tail with a tight median is a different problem from a shifted median.
                    </p>
                    {definition?.limitations && <p>{definition.limitations}</p>}
                    <p className="opacity-70">The last bucket is the overflow — everything above the top edge.</p>
                  </>
                }
                actions={
                  <ExportMenu
                    local={{
                      label: "This chart",
                      filename: `telephony-distribution-${stage}`,
                      columns: ["lower_ms", "upper_ms", "count"],
                      rows: (overview.data.distributions[stage] ?? []) as unknown as Array<
                        Record<string, unknown>
                      >,
                    }}
                  />
                }
              >
                <DistributionChart buckets={overview.data.distributions[stage]} stage={stage} />
              </Block>
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <OutcomeMix
                calls={calls.data?.calls ?? []}
                filters={filters}
                onChange={applyFilters}
                query={query}
              />
              <ComparisonChart
                data={overview.data}
                filters={filters}
                onChange={applyFilters}
                query={query}
              />
            </div>

            <ReliabilityTiles data={overview.data} />

            <Block
              title="Slowest turns"
              info={
                <>
                  <p>
                    The slowest responses in this window. The bottleneck shown is{" "}
                    <strong>measured</strong>, not guessed: it is the stage that owned the largest
                    share of the window between the caller falling silent and the first response
                    audio going out.
                  </p>
                  <p>Click through to open the call at that turn, with its spans and events.</p>
                </>
              }
              actions={<ExportMenu dataset="turns" query={query} />}
            >
              {slowest.isLoading ? (
                <Skeleton className="h-32 rounded-lg" />
              ) : (
                <SlowTurns turns={slowest.data ?? []} />
              )}
            </Block>
          </>
        )}

        <Block
          title="Calls"
          info={
            <>
              <p>
                Every call matching the filters — including calls that never connected and calls
                that were not sampled, so a failure is never missing from this list.
              </p>
              <p>
                Export downloads the whole filtered set, not just the page on screen. Phone numbers
                are masked at write time; no transcript, recording or tool payload exists in this
                data to export.
              </p>
            </>
          }
          actions={<ExportMenu dataset="calls" query={query} />}
        >
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
        </Block>
      </div>
    </PageContainer>
  )
}
