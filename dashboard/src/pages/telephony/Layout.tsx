import { useCallback, useMemo } from "react"
import { Outlet, useSearchParams } from "react-router-dom"
import { PageContainer } from "@/components/layout/PageContainer"
import { ActiveFilters } from "@/components/telephony/ActiveFilters"
import { ExportMenu } from "@/components/telephony/ExportMenu"
import { Filters } from "@/components/telephony/Filters"
import { InfoHint } from "@/components/telephony/InfoHint"
import { TelephonyTabs } from "@/components/telephony/TelephonyTabs"
import { filterParams, useTelephonyAlerts, useTelephonyCalls } from "@/api/hooks/useTelephony"
import type { TelephonyFilters } from "@/api/types"
import type { TelephonyContext } from "./context"

const STRING_KEYS = [
  "environment",
  "app_version",
  "direction",
  "provider",
  "engine",
  "model",
  "language",
  "status",
  "failure_category",
  "failure_code",
  "search",
] as const

/**
 * Shell for the Telephony sections: filters, tab nav, shared state.
 *
 * Filters live in the URL rather than in component state. That makes them
 * survive a tab switch and a reload, and makes "the latency view, German
 * outbound calls, last 6h" a link someone can paste into an incident channel.
 */
export function TelephonyLayout() {
  const [searchParams, setSearchParams] = useSearchParams()

  const filters = useMemo<TelephonyFilters>(() => {
    const parsed: TelephonyFilters = { hours: Number(searchParams.get("hours")) || 24 }
    for (const key of STRING_KEYS) {
      const value = searchParams.get(key)
      if (value) parsed[key] = value
    }
    return parsed
  }, [searchParams])

  const setFilters = useCallback(
    (next: TelephonyFilters) => {
      const params = new URLSearchParams()
      if (next.hours !== 24) params.set("hours", String(next.hours))
      for (const key of STRING_KEYS) {
        const value = next[key]
        if (value) params.set(key, String(value))
      }
      // `replace` keeps the browser's back button meaning "the previous page",
      // not "the previous keystroke in the search box".
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  const query = useMemo(() => filterParams(filters), [filters])

  // Only for the tab badges — the sections fetch their own data.
  const alerts = useTelephonyAlerts()
  const calls = useTelephonyCalls(filters, { limit: 1, offset: 0, sort: "registered_at", order: "desc" })

  const context: TelephonyContext = { filters, setFilters, query }

  return (
    <PageContainer title="Telephony">
      <div className="space-y-4">
        <div className="space-y-2">
          <FiltersRow filters={filters} setFilters={setFilters} />
          <div className="flex items-center gap-1">
            <ActiveFilters filters={filters} onChange={setFilters} />
            <div className="ml-auto flex items-center gap-1">
              <InfoHint title="Downloading this window">
                <p>
                  Every download covers the <strong>whole filtered window</strong>, not the page on
                  screen or the section you are looking at.
                </p>
                <p>
                  The archive is the one to take for analysis: the aggregate, one row per call, one
                  row per turn, the per-stage percentiles, and the metric definitions needed to read
                  any of it — with the filter that produced it recorded alongside.
                </p>
                <p>
                  Technical telemetry only. Numbers are masked and no transcript, recording, prompt
                  or tool payload exists in these tables to export.
                </p>
              </InfoHint>
              <ExportMenu
                label="Download"
                dataset="overview"
                query={query}
                archive={{
                  label: "This window · everything",
                  items: [
                    {
                      label: "Full archive · ZIP",
                      path: `api/telephony/export/archive?${new URLSearchParams(query)}`,
                      filename: "telephony-export.zip",
                    },
                  ],
                }}
              />
            </div>
          </div>
        </div>

        <TelephonyTabs
          firingAlerts={(alerts.data ?? []).filter((alert) => alert.firing).length}
          callCount={calls.data?.total}
        />

        <Outlet context={context} />
      </div>
    </PageContainer>
  )
}

type Facet = "environment" | "app_version" | "model" | "language"

const FACET_PAGE = { limit: 200, offset: 0, sort: "registered_at", order: "desc" }

/** Every current filter except one facet — that facet's own selection must not
 *  narrow its own option list. */
function without(filters: TelephonyFilters, facet: Facet): TelephonyFilters {
  const rest = { ...filters }
  delete rest[facet]
  return rest
}

/** Facet values come from the calls currently in scope, so the dropdowns only
 *  ever offer values that exist.
 *
 *  Each list is derived from the calls matching every filter EXCEPT its own
 *  facet. Deriving all four from the fully-filtered rows made each dropdown a
 *  one-way door: pick `environment=prod` and the fetch returns only prod rows,
 *  so the environment list collapses to `["prod"]` and staging and dev are
 *  unreachable until every filter is cleared — the opposite of the goal.
 *
 *  The extra queries are nearly free: the params are the query key, so while a
 *  facet is unset its query is identical to the others and react-query serves
 *  all of them from one cache entry. Only a facet that is actually set costs a
 *  request.
 *
 *  Cross-facet narrowing still applies — with `model=X` set, the environment
 *  list covers only environments that have an X call — which is what keeps the
 *  offer honest. The current selection is always included so the control can
 *  still show and clear itself; a native <select> whose value is missing from
 *  its options renders blank. */
function FiltersRow({
  filters,
  setFilters,
}: {
  filters: TelephonyFilters
  setFilters: (next: TelephonyFilters) => void
}) {
  const byEnvironment = useTelephonyCalls(without(filters, "environment"), FACET_PAGE)
  const byVersion = useTelephonyCalls(without(filters, "app_version"), FACET_PAGE)
  const byModel = useTelephonyCalls(without(filters, "model"), FACET_PAGE)
  const byLanguage = useTelephonyCalls(without(filters, "language"), FACET_PAGE)

  const unique = (query: ReturnType<typeof useTelephonyCalls>, key: Facet) => {
    const values = (query.data?.calls ?? []).map((row) => String(row[key] ?? ""))
    const selected = filters[key]
    if (selected) values.push(String(selected))
    return [...new Set(values.filter(Boolean))].sort()
  }

  return (
    <Filters
      filters={filters}
      onChange={setFilters}
      environments={unique(byEnvironment, "environment")}
      versions={unique(byVersion, "app_version")}
      models={unique(byModel, "model")}
      languages={unique(byLanguage, "language")}
    />
  )
}
