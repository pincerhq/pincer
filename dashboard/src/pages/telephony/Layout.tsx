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

/** Facet values come from the calls currently in scope, so the dropdowns only
 *  ever offer values that exist. */
function FiltersRow({
  filters,
  setFilters,
}: {
  filters: TelephonyFilters
  setFilters: (next: TelephonyFilters) => void
}) {
  const calls = useTelephonyCalls(filters, { limit: 200, offset: 0, sort: "registered_at", order: "desc" })
  const rows = calls.data?.calls ?? []
  const unique = (key: "environment" | "app_version" | "model" | "language") =>
    [...new Set(rows.map((row) => String(row[key] ?? "")).filter(Boolean))].sort()

  return (
    <Filters
      filters={filters}
      onChange={setFilters}
      environments={unique("environment")}
      versions={unique("app_version")}
      models={unique("model")}
      languages={unique("language")}
    />
  )
}
