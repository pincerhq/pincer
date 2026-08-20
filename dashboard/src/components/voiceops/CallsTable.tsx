import type { VoiceCallSummary } from "@/api/types"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import { formatDateTime } from "@/lib/formatters"
import { cn } from "@/lib/utils"

/** Codes where the callee was simply unreachable — not our failure. */
const NOT_OUR_FAULT = new Set(["no_answer", "busy", "voicemail", "wrong_number"])

function FailureBadge({ call }: { call: VoiceCallSummary }) {
  if (!call.failure_code) return <span className="text-[var(--color-muted)]">—</span>
  if (call.failure_code === "none") {
    return <span className="text-emerald-400">completed</span>
  }
  const neutral = NOT_OUR_FAULT.has(call.failure_code)
  return (
    <span
      className={cn("font-mono text-xs", neutral ? "text-amber-400" : "text-red-400")}
      title={call.failure_description}
    >
      {call.failure_code}
    </span>
  )
}

interface CallsTableProps {
  calls: VoiceCallSummary[]
  loading?: boolean
}

export function CallsTable({ calls, loading }: CallsTableProps) {
  if (loading) return <Skeleton className="h-64 rounded-xl" />

  if (calls.length === 0) {
    return (
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6 text-center text-sm text-[var(--color-muted)]">
        No calls recorded yet.
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
            <TableHead>Duration</TableHead>
            <TableHead>Outcome</TableHead>
            <TableHead className="text-right">Cost</TableHead>
            <TableHead>Call SID</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {calls.map((call) => (
            <TableRow key={call.call_sid}>
              <TableCell className="whitespace-nowrap text-xs">
                {formatDateTime(call.started_at)}
              </TableCell>
              <TableCell className="text-xs">{call.direction}</TableCell>
              <TableCell className="tabular-nums text-xs">{call.duration_seconds}s</TableCell>
              <TableCell>
                <FailureBadge call={call} />
              </TableCell>
              <TableCell className="text-right tabular-nums text-xs">
                {call.cost_usd == null ? (
                  <span className="text-[var(--color-muted)]">—</span>
                ) : (
                  `$${call.cost_usd.toFixed(3)}`
                )}
              </TableCell>
              <TableCell className="font-mono text-[11px] text-[var(--color-muted)]">
                {call.call_sid}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
