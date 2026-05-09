import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import type { Settings } from "@/api/types"

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      try {
        return await pincer.settings()
      } catch (err) {
        const status = (err as { response?: { status?: number } })?.response?.status
        if (status === 404 || status === 503) return null
        throw err
      }
    },
    retry: false,
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
