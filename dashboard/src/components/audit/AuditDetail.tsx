import type { AuditEntry } from "@/api/types"
import { formatDateTime } from "@/lib/formatters"

interface AuditDetailProps {
  entry: AuditEntry
}

export function AuditDetail({ entry }: AuditDetailProps) {
  return (
    <div className="p-4 bg-[var(--color-background)] rounded-lg border border-[var(--color-border)] space-y-3 text-xs">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <span className="text-[var(--color-muted)] uppercase tracking-wider">
            Timestamp
          </span>
          <p className="mt-1 font-mono">{formatDateTime(entry.timestamp)}</p>
        </div>
        <div>
          <span className="text-[var(--color-muted)] uppercase tracking-wider">
            Duration
          </span>
          <p className="mt-1 font-mono">
            {entry.duration_ms != null ? `${entry.duration_ms}ms` : "—"}
          </p>
        </div>
        {entry.cost_usd != null && (
          <div>
            <span className="text-[var(--color-muted)] uppercase tracking-wider">
              Cost
            </span>
            <p className="mt-1 font-mono">${entry.cost_usd.toFixed(6)}</p>
          </div>
        )}
      </div>

      {entry.input_summary && (
        <div>
          <span className="text-[var(--color-muted)] uppercase tracking-wider">
            Input
          </span>
          <pre className="mt-1 p-2 rounded bg-white/[0.02] font-mono text-[var(--color-foreground)] whitespace-pre-wrap break-words">
            {entry.input_summary}
          </pre>
        </div>
      )}

      {entry.output_summary && (
        <div>
          <span className="text-[var(--color-muted)] uppercase tracking-wider">
            Output
          </span>
          <pre className="mt-1 p-2 rounded bg-white/[0.02] font-mono text-[var(--color-foreground)] whitespace-pre-wrap break-words">
            {entry.output_summary}
          </pre>
        </div>
      )}

      {entry.metadata && Object.keys(entry.metadata).length > 0 && (
        <div>
          <span className="text-[var(--color-muted)] uppercase tracking-wider">
            Metadata
          </span>
          <pre className="mt-1 p-2 rounded bg-white/[0.02] font-mono text-[var(--color-foreground)] whitespace-pre-wrap break-words">
            {JSON.stringify(entry.metadata, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
