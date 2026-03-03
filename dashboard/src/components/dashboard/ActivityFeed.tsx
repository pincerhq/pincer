import { useAudit } from "@/api/hooks/useAudit"
import { formatRelative } from "@/lib/formatters"
import { Skeleton } from "@/components/ui/skeleton"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const ACTION_STYLES: Record<string, string> = {
  message_received: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  tool_call: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  llm_request: "bg-purple-500/10 text-purple-400 border-purple-500/20",
  error: "bg-red-500/10 text-red-400 border-red-500/20",
}

export function ActivityFeed() {
  const { data, isLoading } = useAudit({ limit: "10" })

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
      <p className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
        Recent Activity
      </p>
      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="flex items-center gap-3">
              <Skeleton className="h-5 w-20 bg-white/[0.06]" />
              <Skeleton className="h-4 flex-1 bg-white/[0.06]" />
              <Skeleton className="h-3 w-16 bg-white/[0.06]" />
            </div>
          ))}
        </div>
      ) : !data?.entries.length ? (
        <p className="text-sm text-[var(--color-muted)]">No recent activity</p>
      ) : (
        <div className="space-y-2">
          {data.entries.map((entry) => (
            <div
              key={entry.id}
              className="flex items-center gap-3 py-1.5 text-sm"
            >
              <Badge
                variant="outline"
                className={cn(
                  "text-[10px] font-mono shrink-0",
                  ACTION_STYLES[entry.action] ?? "border-white/10 text-[var(--color-muted)]",
                )}
              >
                {entry.action.replace(/_/g, " ")}
              </Badge>
              <span className="text-[var(--color-foreground)] truncate flex-1">
                {entry.tool
                  ? `${entry.tool}: ${entry.input_summary ?? ""}`
                  : entry.input_summary ?? entry.output_summary ?? ""}
              </span>
              <span className="text-xs text-[var(--color-muted)] shrink-0 font-mono">
                {formatRelative(entry.timestamp)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
