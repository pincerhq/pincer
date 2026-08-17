import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useSchedules(includePast: boolean) {
  return useQuery({
    queryKey: ["schedules", includePast],
    queryFn: () =>
      pincer.schedules(includePast ? { include_past: "true" } : undefined),
    refetchInterval: REFETCH_INTERVALS.SCHEDULES,
  })
}
