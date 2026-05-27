import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useConversations(params?: Record<string, string>) {
  return useInfiniteQuery({
    queryKey: ["conversations", params],
    queryFn: async ({ pageParam }) => {
      try {
        return await pincer.conversations({ ...params, offset: String(pageParam) })
      } catch (err) {
        const res = (err as { response?: { status?: number } })?.response
        if (res?.status === 404) {
          return { conversations: [], total: 0, limit: 20, offset: pageParam as number }
        }
        throw err
      }
    },
    getNextPageParam: (lastPage) => {
      const limit = lastPage.limit ?? 20
      if (lastPage.conversations.length < limit) return undefined
      return (lastPage.offset ?? 0) + lastPage.conversations.length
    },
    initialPageParam: 0,
    enabled: !!params,
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
