import { Link } from "react-router-dom"
import { ArrowRight } from "lucide-react"
import { Skeleton } from "@/components/ui/skeleton"
import { AlertStrip } from "@/components/telephony/AlertStrip"
import { CoverageLine } from "@/components/telephony/CoverageLine"
import { KpiStrip } from "@/components/telephony/KpiStrip"
import { LatencyHero } from "@/components/telephony/LatencyHero"
import { SlowTurns } from "@/components/telephony/SlowTurns"
import { Block } from "@/components/telephony/shared"
import { useSlowestTurns, useTelephonyAlerts, useTelephonyMetricDefinitions, useTelephonyOverview } from "@/api/hooks/useTelephony"
import { ROUTES } from "@/lib/constants"
import { useTelephonyContext } from "./context"

/** Is it healthy, how fast is it, and which call should I open first. */
export function TelephonyOverview() {
  const { filters, setFilters, query } = useTelephonyContext()
  const overview = useTelephonyOverview(filters)
  const alerts = useTelephonyAlerts()
  const definitions = useTelephonyMetricDefinitions()
  const slowest = useSlowestTurns(filters, 4)

  if (overview.isLoading || !overview.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-20 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <CoverageLine health={overview.data.telemetry} coverage={overview.data.coverage} />

      {alerts.isLoading ? <Skeleton className="h-9 rounded-lg" /> : <AlertStrip alerts={alerts.data ?? []} />}

      <KpiStrip data={overview.data} filters={filters} onChange={setFilters} />

      <LatencyHero
        data={overview.data}
        definition={definitions.data?.find((d) => d.key === "response_latency_ms")}
        query={query}
      />

      <Block
        title="Slowest turns"
        info={
          <p>
            The worst responses in this window, with the stage that{" "}
            <strong>measurably</strong> owned the largest share of the wait. Full stage breakdown is
            on the Latency tab.
          </p>
        }
        actions={
          <Link
            to={{ pathname: ROUTES.TELEPHONY_LATENCY, search: window.location.search }}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] text-[var(--color-muted)] hover:bg-white/[0.06] hover:text-[var(--color-foreground)]"
          >
            Latency detail <ArrowRight className="h-3 w-3" />
          </Link>
        }
      >
        {slowest.isLoading ? <Skeleton className="h-28 rounded-lg" /> : <SlowTurns turns={slowest.data ?? []} />}
      </Block>
    </div>
  )
}
