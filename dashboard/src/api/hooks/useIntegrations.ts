import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useIntegrations() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: async () => {
      try {
        return await pincer.integrations()
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return { integrations: [] }
        }
        throw err
      }
    },
    refetchInterval: REFETCH_INTERVALS.SKILLS,
  })
}
