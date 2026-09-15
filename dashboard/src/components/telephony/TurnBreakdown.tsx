import type { TelephonyMetricDefinition, TelephonyTurn } from "@/api/types"
import { cn } from "@/lib/utils"
import { CriticalPathBar } from "./CriticalPathBar"
import { METRIC_LABELS, Ms, stageLabel } from "./shared"

const STAGE_KEYS: Array<keyof TelephonyTurn> = [
  "endpointing_ms",
  "stt_first_partial_ms",
  "stt_final_ms",
  "agent_queue_ms",
  "agent_prep_ms",
  "llm_ttft_ms",
  "llm_total_ms",
  "tool_total_ms",
  "tts_first_audio_ms",
  "tts_total_ms",
  "audio_queue_ms",
  "total_ms",
]

/**
 * Turn-by-turn latency, with the selected turn expanded into its stages and
 * measured critical path.
 *
 * Stage durations here overlap (LLM and TTS run concurrently in a streaming
 * turn). Only the critical path is a partition, and only it may be read as
 * "where the time went".
 */
export function TurnBreakdown({
  turns,
  definitions,
  selectedTurnId,
  onSelect,
  engine,
}: {
  turns: TelephonyTurn[]
  definitions: TelephonyMetricDefinition[]
  selectedTurnId: string
  onSelect: (turnId: string) => void
  engine: string
}) {
  const defsByKey = new Map(definitions.map((d) => [d.key, d]))
  const responseDef = defsByKey.get("response_latency_ms")
  if (!turns.length) {
    return (
      <p className="text-xs text-[var(--color-muted)]">
        No turn telemetry for this call. Either the call ended before a caller utterance, or it was
        not sampled.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      {turns.map((turn) => {
        const open = selectedTurnId === turn.turn_id
        return (
          <div
            key={turn.turn_id}
            className={cn(
              "rounded-lg border border-[var(--color-border)]",
              open && "border-[var(--color-accent)]/40 bg-white/[0.02]",
            )}
          >
            <button
              onClick={() => onSelect(open ? "" : turn.turn_id)}
              className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-xs"
            >
              <span className="font-medium">Turn {turn.turn_no}</span>
              {turn.cancelled && (
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">
                  {turn.interrupted ? "barge-in" : "cancelled"}
                </span>
              )}
              {turn.error && (
                <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] text-red-400">
                  {turn.error}
                </span>
              )}
              {turn.tool_calls > 0 && (
                <span className="text-[10px] text-[var(--color-muted)]">
                  {turn.tool_calls} tool{turn.tool_calls > 1 ? "s" : ""}
                  {turn.tool_timeouts > 0 && <span className="text-amber-400"> · {turn.tool_timeouts} timed out</span>}
                  {turn.tool_retries > 0 && <span className="text-amber-400"> · {turn.tool_retries} retried</span>}
                </span>
              )}
              {!turn.complete && (
                <span
                  className="text-[10px] text-amber-400"
                  title="No first-response-audio measurement for this turn"
                >
                  incomplete telemetry
                </span>
              )}
              <span className="ml-auto flex items-center gap-2">
                {turn.bottleneck_stage && (
                  <span className="text-[10px] text-[var(--color-muted)]">
                    {stageLabel(turn.bottleneck_stage)}
                  </span>
                )}
                <span className="font-semibold tabular-nums">
                  <Ms value={turn.response_latency_ms} />
                </span>
              </span>
            </button>

            {open && (
              <div className="space-y-3 border-t border-[var(--color-border)] px-3 py-3">
                <div>
                  <h4 className="mb-1.5 text-[11px] font-medium text-[var(--color-muted)]">
                    Critical path to first response audio
                  </h4>
                  <CriticalPathBar path={turn.critical_path} />
                  {/* Where the clock STOPS is the metric's own statement: the
                      `response_latency_ms` definition in schema.py already says
                      it ends at the first response audio on Media Streams or the
                      first text token on ConversationRelay, and says it is SENT
                      rather than heard. This used to paraphrase that from a
                      hardcoded engine name, which meant one sentence maintained
                      in two repos and a new engine needing a code change here.
                      The stage block below was already read off `definitions`;
                      this now matches it.

                      Where the clock STARTS stays local — which boundary actually
                      applied is per-turn data (`response_latency_source`) that a
                      metric definition cannot know. */}
                  <p className="mt-1.5 text-[10px] text-[var(--color-muted)]">
                    Clock starts at{" "}
                    {turn.response_latency_source === "speech_end"
                      ? "the caller's measured speech end (Deepgram word timings)"
                      : "transcript arrival — this engine does not expose speech end, so the endpointing wait is NOT included and the real figure is larger"}
                    .{responseDef ? ` ${responseDef.limitations}` : ""}
                  </p>
                </div>

                <div>
                  <h4 className="mb-1.5 text-[11px] font-medium text-[var(--color-muted)]">Stage durations</h4>
                  <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
                    {STAGE_KEYS.map((key) => {
                      const def = defsByKey.get(key as string)
                      const unavailable = def && !def.available_on.includes(engine)
                      return (
                        <div key={key} className="flex items-baseline gap-2 text-[11px]">
                          <span className="text-[var(--color-muted)]">
                            {METRIC_LABELS[key as string] ?? key}
                          </span>
                          <span className="ml-auto tabular-nums">
                            {unavailable ? (
                              <span
                                className="text-[var(--color-muted)]"
                                title={def?.unavailable_reason}
                              >
                                Unavailable
                              </span>
                            ) : (
                              <Ms value={turn[key] as number | null} />
                            )}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                  <p className="mt-1.5 text-[10px] text-[var(--color-muted)]">
                    These overlap; do not add them up. `total_ms` includes work that happened AFTER the
                    first response audio (the rest of the LLM stream, the remaining sentences), which is
                    why it exceeds the response latency.
                  </p>
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
