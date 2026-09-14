import type { VoiceActiveCall } from "@/api/types"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ListenIn } from "@/components/voiceops/ListenIn"
import { formatDateTime } from "@/lib/formatters"

function counterparty(call: VoiceActiveCall): string {
  if (call.direction === "outbound") return call.target_name || call.target_number || "—"
  return call.caller_number || "—"
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}:${String(s).padStart(2, "0")}`
}

interface ActiveCallsProps {
  calls: VoiceActiveCall[]
  loading?: boolean
}

/** Live calls with the 🎧 Listen control (Sprint 15). */
export function ActiveCalls({ calls, loading }: ActiveCallsProps) {
  if (loading) return <Skeleton className="h-20 rounded-xl" />

  if (calls.length === 0) {
    return (
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6 text-center text-sm text-[var(--color-muted)]">
        No active calls right now.
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Started</TableHead>
            <TableHead>Direction</TableHead>
            <TableHead>Counterparty</TableHead>
            <TableHead>Language</TableHead>
            <TableHead>Duration</TableHead>
            <TableHead>Call SID</TableHead>
            <TableHead className="w-[28rem]">Listen</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {calls.map((call) => (
            <TableRow key={call.call_sid} data-testid={`active-call-${call.call_sid}`}>
              <TableCell className="whitespace-nowrap text-xs">{formatDateTime(call.started_at)}</TableCell>
              <TableCell className="text-xs">{call.direction}</TableCell>
              <TableCell className="text-xs" title={call.purpose || call.briefing_task_preview || undefined}>
                {counterparty(call)}
              </TableCell>
              <TableCell className="text-xs uppercase">{call.language}</TableCell>
              <TableCell className="tabular-nums text-xs">{formatDuration(call.duration_seconds)}</TableCell>
              <TableCell className="font-mono text-[11px] text-[var(--color-muted)]">{call.call_sid}</TableCell>
              <TableCell>
                <ListenIn call={call} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
