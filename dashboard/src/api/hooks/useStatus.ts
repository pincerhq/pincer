import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useStatus() {
  return useQuery({
    queryKey: ["status"],
    queryFn: async () => {
      try {
        return await pincer.status()
      } catch {
        return null
      }
    },
    refetchInterval: REFETCH_INTERVALS.STATUS,
  })
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: pincer.health,
    retry: false,
  })
}
