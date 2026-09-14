import type { TelephonyOverview } from "@/api/types"
import { cn } from "@/lib/utils"
import { InfoHint } from "./InfoHint"
import { Block } from "./shared"

const TILES: Array<{ key: string; label: string; info: string; bad?: boolean }> = [
  { key: "errors", label: "Errors", info: "Error events raised anywhere in the call pipeline, tagged with the stage that raised them.", bad: true },
  { key: "timeouts", label: "Timeouts", info: "A stage ran out of time. For tools this is an OUTCOME (defer + post-call follow-up), not a crash.", bad: true },
  { key: "retries", label: "Retries", info: "A component was retried — a resumed TTS segment or a re-run tool call.", bad: true },
  { key: "reconnects", label: "Reconnects", info: "A provider stream died and was re-established mid-call. One reconnect is recovery; a pattern is a problem.", bad: true },
  { key: "barge_ins", label: "Barge-ins", info: "The caller talked over the agent. Not a defect — but a rising rate usually means the agent is too slow or too verbose." },
  { key: "buffer_clears", label: "Buffer clears", info: "Outbound audio we dropped on barge-in. Twilio does not acknowledge a clear, so this is when WE stopped sending." },
  { key: "audio_gaps", label: "Audio gaps", info: "Inbound media frames stopped arriving for >120 ms. Could be network, provider buffering, or our own event loop being blocked — it does not attribute blame.", bad: true },
  { key: "tool_calls", label: "Tool calls", info: "Tools executed during calls, counted per call and per attempt." },
  { key: "tool_timeouts", label: "Tool timeouts", info: "Tools that exceeded PINCER_VOICE_TOOL_TIMEOUT_S and were deferred to a post-call follow-up.", bad: true },
  { key: "cancelled_turns", label: "Cancelled turns", info: "Turns abandoned mid-flight, almost always barge-in. They have no response latency and never enter the percentiles." },
]

/** Counters that say whether the pipeline is coping, with the "why" folded away. */
export function ReliabilityTiles({ data }: { data: TelephonyOverview }) {
  return (
    <Block
      title="Reliability"
      info={
        <p>
          Counts over the selected window, not rates. A timeout is rare enough that "three this hour"
          is the signal; a rate would hide three bad minutes inside a busy hour.
        </p>
      }
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {TILES.map((tile) => {
          const value = data.reliability[tile.key] ?? 0
          return (
            <div key={tile.key} className="rounded-lg border border-[var(--color-border)] px-2.5 py-2">
              <div className="flex items-center gap-1">
                <span className="truncate text-[10px] text-[var(--color-muted)]">{tile.label}</span>
                <InfoHint title={tile.label} className="ml-auto">
                  <p>{tile.info}</p>
                </InfoHint>
              </div>
              <div
                className={cn(
                  "mt-0.5 text-lg font-semibold tabular-nums",
                  tile.bad && value > 0 ? "text-amber-400" : "text-[var(--color-foreground)]",
                )}
              >
                {value}
              </div>
            </div>
          )
        })}
      </div>
    </Block>
  )
}
