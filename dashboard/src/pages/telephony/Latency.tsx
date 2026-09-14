import { useState } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { ComparisonChart } from "@/components/telephony/ComparisonChart"
import { ExportMenu } from "@/components/telephony/ExportMenu"
import { DistributionChart } from "@/components/telephony/LatencyCharts"
import { SlowTurns } from "@/components/telephony/SlowTurns"
import { StageBars } from "@/components/telephony/StageBars"
import { Block, METRIC_LABELS } from "@/components/telephony/shared"
import { useSlowestTurns, useTelephonyMetricDefinitions, useTelephonyOverview } from "@/api/hooks/useTelephony"
import { useTelephonyContext } from "./context"

/** Where the time goes, how it is spread, and who is slower than whom. */
export function TelephonyLatency() {
  const { filters, setFilters, query } = useTelephonyContext()
  const [stage, setStage] = useState("response_latency_ms")
  const overview = useTelephonyOverview(filters)
  const definitions = useTelephonyMetricDefinitions()
  const slowest = useSlowestTurns(filters, 10)

  if (overview.isLoading || !overview.data) {
    return <Skeleton className="h-96 rounded-xl" />
  }

  const definition = definitions.data?.find((d) => d.key === stage)

  return (
    <div className="space-y-4">
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
                How that stage's measurements are spread, not just their percentiles. A long right
                tail with a tight median is a different problem from a shifted median.
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
                rows: (overview.data.distributions[stage] ?? []) as unknown as Array<Record<string, unknown>>,
              }}
            />
          }
        >
          <DistributionChart buckets={overview.data.distributions[stage]} stage={stage} />
        </Block>
      </div>

      <ComparisonChart data={overview.data} filters={filters} onChange={setFilters} query={query} />

      <Block
        title="Slowest turns"
        info={
          <>
            <p>
              The bottleneck shown is <strong>measured</strong>: the stage that owned the largest
              share of the window between the caller falling silent and the first response audio
              going out — not simply the longest span, which in a streaming turn is usually the LLM
              running underneath everything else.
            </p>
            <p>Click through to open that call at that turn, with its spans and events.</p>
          </>
        }
        actions={<ExportMenu dataset="turns" query={query} />}
      >
        {slowest.isLoading ? <Skeleton className="h-32 rounded-lg" /> : <SlowTurns turns={slowest.data ?? []} />}
      </Block>
    </div>
  )
}
