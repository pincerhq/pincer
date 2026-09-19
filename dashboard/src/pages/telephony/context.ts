import { useOutletContext } from "react-router-dom"
import type { TelephonyFilters } from "@/api/types"

export interface TelephonyContext {
  filters: TelephonyFilters
  setFilters: (next: TelephonyFilters) => void
  /** The filters as query params, for the export endpoints. */
  query: Record<string, string>
}

/** Shared filter state for every Telephony section. */
export function useTelephonyContext(): TelephonyContext {
  return useOutletContext<TelephonyContext>()
}
