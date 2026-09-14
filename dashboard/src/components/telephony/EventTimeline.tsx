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
 * Ordered by (UTC timestamp, per-call sequence) at read time, so a Twilio status
 * callback that arrives minutes after teardown still appears where it happened
 * rather than at the bottom.
 */
export function EventTimeline({ events, turnId }: { events: TelephonyEvent[]; turnId?: string }) {
  const [filter, setFilter] = useState("")
  const visible = useMemo(() => {
    let rows = events
    if (turnId) rows = rows.filter((e) => e.turn_id === turnId || !e.turn_id)
    if (filter.trim()) {
      const needle = filter.trim().toLowerCase()
      rows = rows.filter((e) => e.name.toLowerCase().includes(needle))
    }
    return rows
  }, [events, filter, turnId])

  const first = events.length ? new Date(events[0].ts_utc).getTime() : 0

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
              const offset = new Date(event.ts_utc).getTime() - first
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
