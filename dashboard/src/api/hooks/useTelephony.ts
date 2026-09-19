import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"
import type { TelephonyFilters } from "@/api/types"

/** Filters -> query string, dropping empties so the URL stays readable. */
export function filterParams(filters: TelephonyFilters): Record<string, string> {
  const params: Record<string, string> = { hours: String(filters.hours) }
  for (const [key, value] of Object.entries(filters)) {
    if (key === "hours") continue
    if (typeof value === "string" && value.trim()) params[key] = value.trim()
  }
  return params
}

export function useTelephonyOverview(filters: TelephonyFilters) {
  const params = filterParams(filters)
  return useQuery({
    queryKey: ["telephony", "overview", params],
    queryFn: () => pincer.telephonyOverview(params),
    refetchInterval: REFETCH_INTERVALS.TELEPHONY,
  })
}

export function useTelephonyCalls(
  filters: TelephonyFilters,
  page: { limit: number; offset: number; sort: string; order: string },
) {
  const params = {
    ...filterParams(filters),
    limit: String(page.limit),
    offset: String(page.offset),
    sort: page.sort,
    order: page.order,
  }
  return useQuery({
    queryKey: ["telephony", "calls", params],
    queryFn: () => pincer.telephonyCalls(params),
    refetchInterval: REFETCH_INTERVALS.TELEPHONY,
  })
}

export function useTelephonyCall(ref: string | undefined) {
  return useQuery({
    queryKey: ["telephony", "call", ref],
    queryFn: () => pincer.telephonyCall(ref as string),
    enabled: Boolean(ref),
  })
}

export function useTelephonyCallEvents(ref: string | undefined) {
  return useQuery({
    queryKey: ["telephony", "call", ref, "events"],
    queryFn: () => pincer.telephonyCallEvents(ref as string),
    enabled: Boolean(ref),
  })
}

export function useTelephonyCallSpans(ref: string | undefined) {
  return useQuery({
    queryKey: ["telephony", "call", ref, "spans"],
    queryFn: () => pincer.telephonyCallSpans(ref as string),
    enabled: Boolean(ref),
  })
}

export function useSlowestTurns(filters: TelephonyFilters, limit = 10) {
  const params = { ...filterParams(filters), limit: String(limit) }
  return useQuery({
    queryKey: ["telephony", "slowest", params],
    queryFn: () => pincer.telephonySlowestTurns(params),
    refetchInterval: REFETCH_INTERVALS.TELEPHONY,
  })
}

export function useTelephonyAlerts() {
  return useQuery({
    queryKey: ["telephony", "alerts"],
    queryFn: () => pincer.telephonyAlerts(),
    refetchInterval: REFETCH_INTERVALS.TELEPHONY,
  })
}

export function useTelephonyMetricDefinitions() {
  return useQuery({
    queryKey: ["telephony", "metrics"],
    queryFn: () => pincer.telephonyMetricDefinitions(),
    staleTime: 10 * 60_000,
  })
}
