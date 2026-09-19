import { useMemo, useState } from "react"
import type { TelephonyEvent } from "@/api/types"
import { cn } from "@/lib/utils"

const TONE: Array<[RegExp, string]> = [
  [/^error|^timeout/, "text-red-400"],
  [/^reconnect|bargein|cancelled|buffer_cleared|audio\.gap/, "text-amber-400"],
  [/^call\.(ended|declined)/, "text-[var(--color-muted)]"],
  [/^call\./, "text-sky-400"],
  [/^llm\./, "text-indigo-300"],
  [/^tts\.|^audio\./, "text-emerald-400"],
  [/^stt\./, "text-cyan-300"],
  [/^tool\./, "text-amber-300"],
]

function tone(name: string): string {
  for (const [pattern, className] of TONE) if (pattern.test(name)) return className
  return "text-[var(--color-foreground)]"
}

/**
 * The chronological event log for a call.
 *
 * Ordered by (UTC timestamp, per-call sequence), so a Twilio status callback
 * that arrives minutes after teardown appears where it happened rather than at
 * the bottom.
 *
 * That ordering is the backend's — `queries.get_events` selects
 * `ORDER BY ts_utc ASC, seq ASC` — and this component used to simply trust it
 * while reading `events[0]` as the time origin and rendering in array order.
 * The trust was invisible: nothing here said the order was load-bearing, so
 * relaxing that ORDER BY would have silently produced negative offsets
 * rendered as "+-1.23s" and a timeline out of sequence, in the exact
 * late-callback case the ordering exists for.
 *
 * So it sorts by the same key rather than assuming. The sort is over an
 * already-ordered list in every normal case, and `seq` is carried on the wire,
 * so it reproduces the backend's tiebreak exactly instead of degrading it.
 */
export function EventTimeline({ events, turnId }: { events: TelephonyEvent[]; turnId?: string }) {
  const [filter, setFilter] = useState("")

  const ordered = useMemo(
    () =>
      [...events].sort(
        (a, b) => Date.parse(a.ts_utc) - Date.parse(b.ts_utc) || (a.seq ?? 0) - (b.seq ?? 0),
      ),
    [events],
  )

  const visible = useMemo(() => {
    let rows = ordered
    if (turnId) rows = rows.filter((e) => e.turn_id === turnId || !e.turn_id)
    if (filter.trim()) {
      const needle = filter.trim().toLowerCase()
      rows = rows.filter((e) => e.name.toLowerCase().includes(needle))
    }
    return rows
  }, [ordered, filter, turnId])

  // The origin is the call's first event, not the filtered view's, so offsets
  // stay comparable when a turn filter is on.
  const first = ordered.length ? Date.parse(ordered[0].ts_utc) : 0

  return (
    <div className="space-y-2">
      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter events…"
        className="w-full rounded-md border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
      />
      <div className="max-h-[420px] overflow-y-auto">
        <table className="w-full text-xs">
          <tbody>
            {visible.map((event) => {
              const offset = Date.parse(event.ts_utc) - first
              const attrs = Object.entries(event.attributes).filter(([, v]) => v !== null && v !== "")
              return (
                <tr key={event.event_id} className="border-b border-[var(--color-border)]/50 align-top">
                  <td className="w-20 py-1 pr-2 text-right tabular-nums text-[var(--color-muted)]">
                    +{(offset / 1000).toFixed(2)}s
                  </td>
                  <td className={cn("py-1 pr-2 font-mono", tone(event.name))}>{event.name}</td>
                  <td className="py-1 text-[10px] text-[var(--color-muted)]">
                    {attrs.map(([key, value]) => (
                      <span key={key} className="mr-2 whitespace-nowrap">
                        {key}=<span className="text-[var(--color-foreground)]">{String(value)}</span>
                      </span>
                    ))}
                  </td>
                </tr>
              )
            })}
            {!visible.length && (
              <tr>
                <td colSpan={3} className="py-6 text-center text-[var(--color-muted)]">
                  No events recorded for this call.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
