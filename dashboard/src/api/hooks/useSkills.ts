import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useSkills() {
  return useQuery({
    queryKey: ["skills"],
    queryFn: async () => {
      try {
        return await pincer.skills()
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return { skills: [] }
        }
        throw err
      }
    },
    refetchInterval: REFETCH_INTERVALS.SKILLS,
  })
}

export function useSkillScan() {
  return useMutation({
    mutationFn: (path: string) => pincer.skillScan(path),
  })
}

export function useSkillInstall() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (url: string) => pincer.skillInstall(url),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] })
    },
  })
}

export function useSkillDelete() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => pincer.skillDelete(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] })
    },
  })
}
