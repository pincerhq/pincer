import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Download, RefreshCw } from "lucide-react"

interface AuditFiltersProps {
  action: string
  user: string
  onActionChange: (v: string) => void
  onUserChange: (v: string) => void
  onExportCSV: () => void
  onExportJSON: () => void
  onRefresh: () => void
  tailMode: boolean
  onTailToggle: () => void
}

const ACTIONS = [
  "",
  "message_received",
  "tool_call",
  "llm_request",
  "file_access",
  "error",
]

export function AuditFilters({
  action,
  user,
  onActionChange,
  onUserChange,
  onExportCSV,
  onExportJSON,
  onRefresh,
  tailMode,
  onTailToggle,
}: AuditFiltersProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <select
        value={action}
        onChange={(e) => onActionChange(e.target.value)}
        className="h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 text-sm text-[var(--color-foreground)] focus:outline-none focus:border-[var(--color-accent)]"
      >
        <option value="">All actions</option>
        {ACTIONS.filter(Boolean).map((a) => (
          <option key={a} value={a}>
            {a.replace(/_/g, " ")}
          </option>
        ))}
      </select>

      <Input
        placeholder="Filter by user..."
        value={user}
        onChange={(e) => onUserChange(e.target.value)}
        className="w-48 bg-[var(--color-card)] border-[var(--color-border)]"
      />

      <div className="ml-auto flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={onTailToggle}
          className={`border-[var(--color-border)] text-xs ${tailMode ? "bg-[var(--color-accent)]/10 text-[var(--color-accent)] border-[var(--color-accent)]/30" : ""}`}
        >
          {tailMode ? "Tail: ON" : "Tail: OFF"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onRefresh}
          className="border-[var(--color-border)]"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onExportCSV}
          className="border-[var(--color-border)]"
        >
          <Download className="h-3.5 w-3.5 mr-1.5" />
          CSV
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onExportJSON}
          className="border-[var(--color-border)]"
        >
          <Download className="h-3.5 w-3.5 mr-1.5" />
          JSON
        </Button>
      </div>
    </div>
  )
}
