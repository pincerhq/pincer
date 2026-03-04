import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import type { Settings } from "@/api/types"

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: pincer.settings,
  })
}

export function useUpdateSettings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<Settings>) => pincer.updateSettings(data),
    onSuccess: (updated) => {
      queryClient.setQueryData(["settings"], updated)
    },
  })
}

export function useDoctor() {
  return useQuery({
    queryKey: ["doctor"],
    queryFn: pincer.doctor,
    enabled: false,
  })
}
