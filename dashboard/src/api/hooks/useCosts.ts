import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useCostsToday() {
  return useQuery({
    queryKey: ["costs-today"],
    queryFn: pincer.costsToday,
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsHistory(days = 30) {
  return useQuery({
    queryKey: ["costs-history", days],
    queryFn: () => pincer.costsHistory(days),
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsByTool(days = 7) {
  return useQuery({
    queryKey: ["costs-by-tool", days],
    queryFn: () => pincer.costsByTool(days),
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}

export function useCostsByModel(days = 7) {
  return useQuery({
    queryKey: ["costs-by-model", days],
    queryFn: () => pincer.costsByModel(days),
    refetchInterval: REFETCH_INTERVALS.COSTS,
  })
}
