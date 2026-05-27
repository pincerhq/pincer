import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"

export function useIdentities() {
  return useQuery({
    queryKey: ["identities"],
    queryFn: () => pincer.identities(),
    staleTime: 60_000,
  })
}
