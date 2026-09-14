import { useMemo, useState } from "react"
import { Link, useParams, useSearchParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { CallHeader } from "@/components/telephony/CallHeader"
import { EventTimeline } from "@/components/telephony/EventTimeline"
import { Panel, Ms, stageLabel } from "@/components/telephony/shared"
import { TurnBreakdown } from "@/components/telephony/TurnBreakdown"
import { UnavailablePanel } from "@/components/telephony/UnavailablePanel"
import { Waterfall } from "@/components/telephony/Waterfall"
import {
  useTelephonyCall,
  useTelephonyCallEvents,
  useTelephonyCallSpans,
  useTelephonyMetricDefinitions,
} from "@/api/hooks/useTelephony"
import { ROUTES } from "@/lib/constants"
import type { TelephonySpan } from "@/api/types"

/**
 * One call, in full.
 *
 * The order is the order an engineer works in: what the call was → what went
 * wrong → which turn was slow → which spans explain that turn → the raw events.
 */
export function TelephonyCallPage() {
  const { callRef } = useParams<{ callRef: string }>()
  const [searchParams] = useSearchParams()
  const detail = useTelephonyCall(callRef)
  const events = useTelephonyCallEvents(callRef)
  const spans = useTelephonyCallSpans(callRef)
  const definitions = useTelephonyMetricDefinitions()

  const [selectedTurn, setSelectedTurn] = useState(searchParams.get("turn") ?? "")
  const [selectedSpan, setSelectedSpan] = useState<TelephonySpan | null>(null)

  const turnSpans = useMemo(() => {
    const all = spans.data ?? []
    if (!selectedTurn) return all
    return all.filter((span) => span.turn_id === selectedTurn)
  }, [spans.data, selectedTurn])

  if (detail.isLoading || !detail.data) {
    return (
      <PageContainer title="Call">
        <Skeleton className="h-64 rounded-xl" />
      </PageContainer>
    )
  }

  const { call, turns, unavailable, telemetry_gaps: gaps } = detail.data
  const slowest = [...turns]
    .filter((t) => t.response_latency_ms !== null)
    .sort((a, b) => (b.response_latency_ms ?? 0) - (a.response_latency_ms ?? 0))
    .slice(0, 3)

  return (
    <PageContainer title={call.provider_call_id || call.call_id}>
      <div className="space-y-5">
        <Link
          to={ROUTES.TELEPHONY}
          className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted)] hover:text-[var(--color-foreground)]"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> All calls
        </Link>

        <CallHeader call={call} gaps={gaps} />

        {slowest.length > 0 && (
          <Panel
            title="Slowest turns on this call"
            subtitle="Bottleneck is the measured owner of the largest share of the response window."
          >
            <div className="flex flex-wrap gap-2">
              {slowest.map((turn) => (
                <button
                  key={turn.turn_id}
                  onClick={() => setSelectedTurn(turn.turn_id)}
                  className={`rounded-lg border px-3 py-2 text-left text-xs hover:bg-white/[0.04] ${
                    selectedTurn === turn.turn_id
                      ? "border-[var(--color-accent)]/50 bg-white/[0.04]"
                      : "border-[var(--color-border)]"
                  }`}
                >
                  <div className="font-medium">Turn {turn.turn_no}</div>
                  <div className="tabular-nums">
                    <Ms value={turn.response_latency_ms} />
                  </div>
                  <div className="text-[10px] text-[var(--color-muted)]">
                    {stageLabel(turn.bottleneck_stage)}
                  </div>
                </button>
              ))}
            </div>
          </Panel>
        )}

        <Panel
          title="Turn-by-turn latency"
          subtitle="Open a turn for its stage durations and its critical path to the first response audio."
        >
          <TurnBreakdown
            turns={turns}
            definitions={definitions.data ?? []}
            selectedTurnId={selectedTurn}
            onSelect={setSelectedTurn}
            engine={call.engine}
          />
        </Panel>

        <Panel
          title={selectedTurn ? "Waterfall — selected turn" : "Waterfall — whole call"}
          subtitle="Concurrent and sequential spans as they actually ran."
          action={
            selectedTurn ? (
              <button
                onClick={() => setSelectedTurn("")}
                className="rounded px-2 py-1 text-[11px] text-[var(--color-muted)] hover:bg-white/[0.06]"
              >
                Show whole call
              </button>
            ) : undefined
          }
        >
          {spans.isLoading ? (
            <Skeleton className="h-40 rounded-lg" />
          ) : (
            <Waterfall spans={turnSpans} onSelect={setSelectedSpan} selectedSpanId={selectedSpan?.span_id} />
          )}
          {selectedSpan && (
            <div className="mt-3 rounded-lg border border-[var(--color-border)] bg-white/[0.02] p-3 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-medium">{stageLabel(selectedSpan.name)}</span>
                <span className="font-mono text-[10px] text-[var(--color-muted)]">
                  {selectedSpan.span_id}
                </span>
                <span className="ml-auto tabular-nums">
                  <Ms value={selectedSpan.duration_ms} />
                </span>
              </div>
              <div className="mt-1 text-[10px] text-[var(--color-muted)]">
                status {selectedSpan.status} · attempt {selectedSpan.attempt} · starts at{" "}
                {selectedSpan.start_offset_ms.toFixed(1)}ms
              </div>
              {Object.keys(selectedSpan.attributes).length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-2 text-[10px]">
                  {Object.entries(selectedSpan.attributes).map(([key, value]) => (
                    <span key={key} className="rounded bg-white/[0.05] px-1.5 py-0.5">
                      {key}=<span className="text-[var(--color-foreground)]">{String(value)}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </Panel>

        <Panel
          title="Event timeline"
          subtitle="Ordered by (UTC timestamp, per-call sequence) at read time, so late provider callbacks land in place."
        >
          {events.isLoading ? (
            <Skeleton className="h-64 rounded-lg" />
          ) : (
            <EventTimeline events={events.data ?? []} turnId={selectedTurn || undefined} />
          )}
        </Panel>

        <Panel
          title="Not measurable on this engine"
          subtitle={`Engine: ${call.engine || "unknown"}`}
        >
          <UnavailablePanel metrics={unavailable} />
        </Panel>
      </div>
    </PageContainer>
  )
}
