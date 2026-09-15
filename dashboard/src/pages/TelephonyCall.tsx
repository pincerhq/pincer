import { useMemo, useState } from "react"
import { Link, useParams, useSearchParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { CallHeader } from "@/components/telephony/CallHeader"
import { EventTimeline } from "@/components/telephony/EventTimeline"
import { ExportMenu } from "@/components/telephony/ExportMenu"
import { InfoHint } from "@/components/telephony/InfoHint"
import { Block, Ms, stageLabel } from "@/components/telephony/shared"
import { TurnBreakdown } from "@/components/telephony/TurnBreakdown"
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
 * Ordered the way an engineer works: what the call was → which turn was slow →
 * which spans explain that turn → the raw events. Explanations are folded behind
 * "i" icons so the page reads as data rather than as documentation.
 */
function CallDetail() {
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
  const reference = call.provider_call_id || call.call_id
  const slowest = [...turns]
    .filter((t) => t.response_latency_ms !== null)
    .sort((a, b) => (b.response_latency_ms ?? 0) - (a.response_latency_ms ?? 0))
    .slice(0, 3)

  return (
    <PageContainer title={reference}>
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <Link
            to={ROUTES.TELEPHONY}
            className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted)] hover:text-[var(--color-foreground)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> All calls
          </Link>
          <div className="ml-auto flex items-center gap-1">
            <InfoHint title="Exporting this call">
              <p>
                This call's metadata, turns, events and spans — the shape to attach to a bug report.
                The archive adds the timeline as CSVs and the metric definitions needed to read the
                timings; the JSON document is the same data as one file.
              </p>
              <p>
                Both are assembled by the server from the database, so the download is complete even
                if this page is still loading its timeline.
              </p>
              <p>
                Technical telemetry only: masked numbers, stage timings and span names. Transcripts
                and recordings live behind their own permissions and are not part of it.
              </p>
            </InfoHint>
            <ExportMenu
              label="Download call"
              archive={{
                label: "This call · complete",
                items: [
                  {
                    label: "Archive · ZIP",
                    path: `api/telephony/calls/${encodeURIComponent(callRef ?? call.call_id)}/export?format=zip`,
                    filename: `telephony-call-${reference}.zip`,
                  },
                  {
                    label: "Bundle · JSON",
                    path: `api/telephony/calls/${encodeURIComponent(callRef ?? call.call_id)}/export?format=json`,
                    filename: `telephony-call-${reference}.json`,
                  },
                ],
              }}
            />
          </div>
        </div>

        <CallHeader call={call} gaps={gaps} />

        {slowest.length > 0 && (
          <Block
            title="Slowest turns on this call"
            info={
              <p>
                The bottleneck named here is the stage that <strong>measurably</strong> owned the
                largest share of the response window — not the longest span, which in a streaming
                turn is usually the LLM running underneath everything else.
              </p>
            }
          >
            <div className="flex flex-wrap gap-2">
              {slowest.map((turn) => (
                <button
                  key={turn.turn_id}
                  onClick={() => setSelectedTurn(turn.turn_id)}
                  className={`rounded-lg border px-3 py-2 text-left text-xs transition-colors hover:bg-white/[0.04] ${
                    selectedTurn === turn.turn_id
                      ? "border-[var(--color-accent)]/50 bg-white/[0.04]"
                      : "border-[var(--color-border)]"
                  }`}
                >
                  <div className="font-medium">Turn {turn.turn_no}</div>
                  <div className="text-lg font-semibold tabular-nums">
                    <Ms value={turn.response_latency_ms} />
                  </div>
                  <div className="text-[10px] text-[var(--color-muted)]">
                    {stageLabel(turn.bottleneck_stage)}
                  </div>
                </button>
              ))}
            </div>
          </Block>
        )}

        <Block
          title="Turn-by-turn latency"
          info={
            <>
              <p>Open a turn for its stage durations and its measured critical path.</p>
              <p>
                Stage durations overlap; only the critical path is a partition, and only it may be
                read as "where the time went". Its segments sum to the response latency exactly.
              </p>
            </>
          }
          actions={
            <ExportMenu
              local={{
                label: "This call's turns",
                filename: `telephony-turns-${reference}`,
                columns: [
                  "turn_no",
                  "response_latency_ms",
                  "response_latency_source",
                  "llm_ttft_ms",
                  "llm_total_ms",
                  "tool_total_ms",
                  "tts_first_audio_ms",
                  "bottleneck_stage",
                  "bottleneck_ms",
                  "tool_calls",
                  "cancelled",
                  "error",
                ],
                rows: turns as unknown as Array<Record<string, unknown>>,
              }}
            />
          }
        >
          <TurnBreakdown
            turns={turns}
            definitions={definitions.data ?? []}
            selectedTurnId={selectedTurn}
            onSelect={setSelectedTurn}
            engine={call.engine}
          />
        </Block>

        <Block
          title={selectedTurn ? "Waterfall · selected turn" : "Waterfall · whole call"}
          info={
            <>
              <p>
                Spans as they actually ran. They overlap because the pipeline streams — an LLM span
                running underneath a TTS span is the shape of a fast turn, not a bug.
              </p>
              <p>Durations here are not additive. Click a bar for its attributes.</p>
            </>
          }
          actions={
            <>
              {selectedTurn && (
                <button
                  onClick={() => setSelectedTurn("")}
                  className="rounded px-2 py-1 text-[11px] text-[var(--color-muted)] hover:bg-white/[0.06]"
                >
                  Whole call
                </button>
              )}
              <ExportMenu
                local={{
                  label: "Spans",
                  filename: `telephony-spans-${reference}`,
                  columns: [
                    "span_id",
                    "turn_id",
                    "name",
                    "start_offset_ms",
                    "end_offset_ms",
                    "duration_ms",
                    "status",
                    "attempt",
                  ],
                  rows: (spans.data ?? []) as unknown as Array<Record<string, unknown>>,
                }}
              />
            </>
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
                <span className="font-mono text-[10px] text-[var(--color-muted)]">{selectedSpan.span_id}</span>
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
        </Block>

        <Block
          title="Event timeline"
          info={
            <>
              <p>
                Ordered by (UTC timestamp, per-call sequence) at read time, so a provider callback
                that lands minutes after teardown still appears where it happened.
              </p>
              <p>
                Duplicate provider callbacks collapse to one row — Twilio retries status callbacks
                for minutes, and a retry is not a second event.
              </p>
            </>
          }
          actions={
            <ExportMenu
              local={{
                label: "Events",
                filename: `telephony-events-${reference}`,
                columns: ["ts_utc", "seq", "name", "turn_id", "span_id"],
                rows: (events.data ?? []) as unknown as Array<Record<string, unknown>>,
              }}
            />
          }
        >
          {events.isLoading ? (
            <Skeleton className="h-64 rounded-lg" />
          ) : (
            <EventTimeline events={events.data ?? []} turnId={selectedTurn || undefined} />
          )}
        </Block>

        {unavailable.length > 0 && (
          <div className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2 text-[11px] text-[var(--color-muted)]">
            <span>
              {unavailable.length} metric{unavailable.length === 1 ? "" : "s"} cannot be measured on{" "}
              <span className="font-mono">{call.engine || "this engine"}</span>
            </span>
            <InfoHint title="Not measurable on this engine" className="ml-auto">
              <p>These are structural limits of the pipeline, not gaps in the data:</p>
              <ul className="space-y-1">
                {unavailable.map((metric) => (
                  <li key={metric.key}>
                    <span className="text-[var(--color-foreground)]">{metric.label}</span> —{" "}
                    {metric.unavailable_reason}
                  </li>
                ))}
              </ul>
            </InfoHint>
          </div>
        )}
      </div>
    </PageContainer>
  )
}


/**
 * Remounts the detail on a different call — which is what makes the selection
 * state correct.
 *
 * `selectedTurn` is seeded from `?turn=` by a `useState` initializer and
 * `selectedSpan` is picked by clicking the waterfall; both belong to the call
 * they were chosen in. The route (`App.tsx`, `ROUTES.TELEPHONY_CALL`) renders
 * one element for every `:callRef`, so React reconciles the same component
 * across a call change and neither piece of state resets: the waterfall and
 * the turn breakdown end up filtered against a turn id from the call you just
 * left, and the span panel shows a span that is not in this call at all.
 *
 * `AlertStrip` links here without a `turn` param, which is the nastier case —
 * the URL then gives no hint why the waterfall looks empty.
 *
 * Keyed rather than synced with an effect: a key resets ALL per-call state at
 * once, including any added later, whereas an effect only resyncs the fields
 * someone remembered to list (`selectedSpan` has no URL param to sync from).
 * The requested turn is in the key too, so a link to a *different* turn on the
 * call already open re-seeds the selection instead of being ignored.
 */
export function TelephonyCallPage() {
  const { callRef } = useParams<{ callRef: string }>()
  const [searchParams] = useSearchParams()
  return <CallDetail key={`${callRef ?? ""}|${searchParams.get("turn") ?? ""}`} />
}
