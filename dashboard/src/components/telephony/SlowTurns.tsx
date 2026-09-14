import { Link } from "react-router-dom"
import type { TelephonyTurn } from "@/api/types"
import { ROUTES } from "@/lib/constants"
import { CriticalPathBar } from "./CriticalPathBar"
import { Ms, stageLabel } from "./shared"

/**
 * The slowest response turns, each with the stage that actually cost the time.
 *
 * "Bottleneck" here is MEASURED, not guessed: it is the stage that owned the
 * largest share of the window between the caller falling silent and the first
 * response audio going out.
 */
export function SlowTurns({ turns }: { turns: TelephonyTurn[] }) {
  if (!turns.length) {
    return <p className="text-xs text-[var(--color-muted)]">No turns with a measured response latency.</p>
  }
  return (
    <ul className="space-y-2.5">
      {turns.map((turn) => (
        <li key={turn.turn_id} className="rounded-lg border border-[var(--color-border)] px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Link
              to={`${ROUTES.TELEPHONY_CALL.replace(":callRef", turn.provider_call_id || turn.call_id)}?turn=${turn.turn_id}`}
              className="font-mono text-[var(--color-accent)] hover:underline"
            >
              {turn.provider_call_id || turn.call_id.slice(0, 12)}
            </Link>
            <span className="text-[var(--color-muted)]">turn {turn.turn_no}</span>
            <span className="ml-auto font-semibold tabular-nums">
              <Ms value={turn.response_latency_ms} />
            </span>
          </div>
          <div className="mt-1 text-[10px] text-[var(--color-muted)]">
            bottleneck: <span className="text-[var(--color-foreground)]">{stageLabel(turn.bottleneck_stage)}</span>{" "}
            (<Ms value={turn.bottleneck_ms} />) · latency measured from{" "}
            {turn.response_latency_source === "speech_end"
              ? "measured caller speech end"
              : "transcript arrival (endpointing wait not included)"}
          </div>
          <div className="mt-2">
            <CriticalPathBar path={turn.critical_path} compact />
          </div>
        </li>
      ))}
    </ul>
  )
}
