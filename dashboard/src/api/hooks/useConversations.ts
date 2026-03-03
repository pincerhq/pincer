import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useConversations(params?: Record<string, string>) {
  return useQuery({
    queryKey: ["conversations", params],
    queryFn: async () => {
      try {
        return await pincer.conversations(params)
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return { conversations: [], total: 0 }
        }
        throw err
      }
    },
    refetchInterval: REFETCH_INTERVALS.CONVERSATIONS,
  })
}

export function useConversation(id: string) {
  return useQuery({
    queryKey: ["conversation", id],
    queryFn: async () => {
      try {
        return await pincer.conversation(id)
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return null
        }
        throw err
      }
    },
    enabled: !!id,
  })
}
