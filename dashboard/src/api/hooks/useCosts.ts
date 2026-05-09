import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

const emptyCostsToday = {
  date: "",
  total_usd: 0,
  by_model: {} as Record<string, number>,
  by_tool: {} as Record<string, number>,
  request_count: 0,
  budget: { daily_limit: 0, spent_today: 0, spent_pct: 0, remaining: 0 },
}

const emptyCostsHistory = { period_days: 0, data: [], totals: { total_usd: 0, total_requests: 0 } }
const emptyCostsByTool = { period_days: 0, tools: [] }
const emptyCostsByModel = { period_days: 0, models: [] }

export function useCostsToday() {
  return useQuery({
    queryKey: ["costs-today"],
    queryFn: async () => {
      try {
        return await pincer.costsToday()
      } catch {
        return emptyCostsToday
      }
    },
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsHistory(days = 30) {
  return useQuery({
    queryKey: ["costs-history", days],
    queryFn: async () => {
      try {
        return await pincer.costsHistory(days)
      } catch {
        return emptyCostsHistory
      }
    },
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsByTool(days = 7) {
  return useQuery({
    queryKey: ["costs-by-tool", days],
    queryFn: async () => {
      try {
        return await pincer.costsByTool(days)
      } catch {
        return emptyCostsByTool
      }
    },
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsByModel(days = 7) {
  return useQuery({
    queryKey: ["costs-by-model", days],
    queryFn: async () => {
      try {
        return await pincer.costsByModel(days)
      } catch {
        return emptyCostsByModel
      }
    },
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}
