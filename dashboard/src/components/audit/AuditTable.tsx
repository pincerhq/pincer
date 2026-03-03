import { Fragment, useState } from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { ChevronDown, ChevronRight } from "lucide-react"
import type { AuditEntry } from "@/api/types"
import { AuditDetail } from "./AuditDetail"
import { formatDateTime } from "@/lib/formatters"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const ACTION_VARIANT: Record<string, string> = {
  message_received: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  tool_call: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  llm_request: "bg-purple-500/10 text-purple-400 border-purple-500/20",
  file_access: "bg-orange-500/10 text-orange-400 border-orange-500/20",
  error: "bg-red-500/10 text-red-400 border-red-500/20",
}

interface AuditTableProps {
  entries: AuditEntry[]
  loading?: boolean
}

export function AuditTable({ entries, loading }: AuditTableProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (loading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full bg-white/[0.06]" />
        ))}
      </div>
    )
  }

  if (!entries.length) {
    return (
      <p className="text-sm text-[var(--color-muted)] py-12 text-center">
        No audit entries found
      </p>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow className="border-[var(--color-border)] hover:bg-transparent">
          <TableHead className="w-8" />
          <TableHead className="text-[var(--color-muted)]">Time</TableHead>
          <TableHead className="text-[var(--color-muted)]">User</TableHead>
          <TableHead className="text-[var(--color-muted)]">Action</TableHead>
          <TableHead className="text-[var(--color-muted)]">Tool</TableHead>
          <TableHead className="text-[var(--color-muted)]">Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.map((entry) => {
          const isOpen = expanded.has(entry.id)
          return (
            <Fragment key={entry.id}>
              <TableRow
                className="border-[var(--color-border)] hover:bg-white/[0.02] cursor-pointer group"
                onClick={() => toggle(entry.id)}
              >
                <TableCell className="pr-0">
                  {isOpen ? (
                    <ChevronDown className="h-3.5 w-3.5 text-[var(--color-muted)]" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5 text-[var(--color-muted)]" />
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs text-[var(--color-muted)]">
                  {formatDateTime(entry.timestamp)}
                </TableCell>
                <TableCell className="text-sm">{entry.user_id}</TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={cn(
                      "text-[10px] font-mono",
                      ACTION_VARIANT[entry.action] ??
                        "border-white/10 text-[var(--color-muted)]",
                    )}
                  >
                    {entry.action.replace(/_/g, " ")}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-sm text-[var(--color-muted)]">
                  {entry.tool ?? "—"}
                </TableCell>
                <TableCell>
                  <div
                    className={cn(
                      "h-2 w-2 rounded-full",
                      entry.approved
                        ? "bg-[var(--color-success)]"
                        : "bg-[var(--color-danger)]",
                    )}
                  />
                </TableCell>
              </TableRow>
              {isOpen && (
                <TableRow className="border-[var(--color-border)] hover:bg-transparent">
                  <TableCell colSpan={6} className="p-0">
                    <div className="px-4 pb-4">
                      <AuditDetail entry={entry} />
                    </div>
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}
