import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import type { ScheduledTask } from "@/api/types"
import { formatDateTime } from "@/lib/formatters"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const KIND_LABEL: Record<ScheduledTask["kind"], string> = {
  recurring: "Recurring",
  one_time: "One-time",
}

const KIND_VARIANT: Record<ScheduledTask["kind"], string> = {
  recurring: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  one_time: "bg-purple-500/10 text-purple-400 border-purple-500/20",
}

interface SchedulesTableProps {
  tasks: ScheduledTask[]
  loading?: boolean
}

export function SchedulesTable({ tasks, loading }: SchedulesTableProps) {
  if (loading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full bg-white/[0.06]" />
        ))}
      </div>
    )
  }

  if (!tasks.length) {
    return (
      <p className="text-sm text-[var(--color-muted)] py-12 text-center">
        No scheduled tasks found
      </p>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow className="border-[var(--color-border)] hover:bg-transparent">
          <TableHead className="text-[var(--color-muted)]">Name</TableHead>
          <TableHead className="text-[var(--color-muted)]">Type</TableHead>
          <TableHead className="text-[var(--color-muted)]">Schedule</TableHead>
          <TableHead className="text-[var(--color-muted)]">Channel</TableHead>
          <TableHead className="text-[var(--color-muted)]">Enabled</TableHead>
          <TableHead className="text-[var(--color-muted)]">
            Next / Last run
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((task) => {
          const runAt = task.timing === "past" ? task.last_run_at : task.next_run_at
          return (
            <TableRow
              key={task.id}
              className="border-[var(--color-border)] hover:bg-white/[0.02]"
            >
              <TableCell className="text-sm">{task.name}</TableCell>
              <TableCell>
                <Badge
                  variant="outline"
                  className={cn("text-[10px] font-mono", KIND_VARIANT[task.kind])}
                >
                  {KIND_LABEL[task.kind]}
                </Badge>
              </TableCell>
              <TableCell className="font-mono text-xs text-[var(--color-muted)]">
                {task.cron_expr} ({task.timezone})
              </TableCell>
              <TableCell className="text-sm text-[var(--color-muted)]">
                {task.channel}({task.pincer_user_id})
              </TableCell>
              <TableCell>
                <div
                  className={cn(
                    "h-2 w-2 rounded-full",
                    task.enabled
                      ? "bg-[var(--color-success)]"
                      : "bg-[var(--color-danger)]",
                  )}
                />
              </TableCell>
              <TableCell className="font-mono text-xs text-[var(--color-muted)]">
                {runAt ? formatDateTime(runAt) : "—"}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
