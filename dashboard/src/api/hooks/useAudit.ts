import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

const emptyAuditResponse = { entries: [], total: 0 }
const emptyAuditStats = {
  total_entries: 0,
  by_action: {} as Record<string, number>,
  by_tool: {} as Record<string, number>,
  total_cost_usd: 0,
  failed_actions: 0,
}

export function useAudit(params?: Record<string, string>) {
  return useQuery({
    queryKey: ["audit", params],
    queryFn: async () => {
      try {
        return await pincer.audit(params)
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return emptyAuditResponse
        }
        throw err
      }
    },
    refetchInterval: REFETCH_INTERVALS.AUDIT,
  })
}

export function useAuditStats() {
  return useQuery({
    queryKey: ["audit-stats"],
    queryFn: async () => {
      try {
        return await pincer.auditStats()
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return emptyAuditStats
        }
        throw err
      }
    },
  })
}
