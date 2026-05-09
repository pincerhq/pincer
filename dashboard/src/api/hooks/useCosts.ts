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

function is404(err: unknown): boolean {
  return (err as { response?: { status?: number } })?.response?.status === 404
}

export function useCostsToday() {
  return useQuery({
    queryKey: ["costs-today"],
    queryFn: async () => {
      try {
        return await pincer.costsToday()
      } catch (err) {
        if (is404(err)) return emptyCostsToday
        throw err
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
      } catch (err) {
        if (is404(err)) return emptyCostsHistory
        throw err
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
      } catch (err) {
        if (is404(err)) return emptyCostsByTool
        throw err
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
      } catch (err) {
        if (is404(err)) return emptyCostsByModel
        throw err
      }
    },
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}
