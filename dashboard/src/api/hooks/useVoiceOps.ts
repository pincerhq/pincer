import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { REFETCH_INTERVALS } from "@/lib/constants"

export function useGoldenSignals() {
  return useQuery({
    queryKey: ["ops", "signals"],
    queryFn: () => pincer.opsSignals(),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS,
  })
}

export function useOpsAlerts() {
  return useQuery({
    queryKey: ["ops", "alerts"],
    queryFn: () => pincer.opsAlerts(),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS,
  })
}

export function useSlo() {
  return useQuery({
    queryKey: ["ops", "slo"],
    queryFn: () => pincer.opsSlo(),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS * 4,
  })
}

export function useFailureBreakdown(hours: number) {
  return useQuery({
    queryKey: ["ops", "failures", hours],
    queryFn: () => pincer.opsFailures(hours),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS * 4,
  })
}

export function useCanaryRuns() {
  return useQuery({
    queryKey: ["ops", "canary"],
    queryFn: () => pincer.opsCanary(10),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS * 4,
  })
}

export function useVoiceCalls(limit = 25) {
  return useQuery({
    queryKey: ["ops", "calls", limit],
    queryFn: () => pincer.voiceCalls(limit),
    refetchInterval: REFETCH_INTERVALS.VOICE_OPS,
  })
}

/** Live calls, including whether the listen-in fork is attached. Polls fast:
 *  a call that just connected should show its 🎧 button within seconds. */
export function useActiveCalls() {
  return useQuery({
    queryKey: ["ops", "active"],
    queryFn: () => pincer.voiceActive(),
    refetchInterval: 5_000,
  })
}

/** Places a REAL phone call — the button that uses this must say so. */
export function useTriggerCanary() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => pincer.triggerCanary(),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["ops", "canary"] })
      void queryClient.invalidateQueries({ queryKey: ["ops", "signals"] })
    },
  })
}
