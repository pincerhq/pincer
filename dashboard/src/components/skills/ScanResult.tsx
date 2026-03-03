import type { ScanResult as ScanResultType } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

interface ScanResultProps {
  result: ScanResultType
}

const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-red-500/10 text-red-400 border-red-500/20",
  warning: "bg-yellow-500/10 text-yellow-400 border-yellow-500/20",
  info: "bg-blue-500/10 text-blue-400 border-blue-500/20",
}

export function ScanResult({ result }: ScanResultProps) {
  const scoreColor =
    result.score >= 80
      ? "text-[var(--color-success)]"
      : result.score >= 50
        ? "text-[var(--color-warning)]"
        : "text-[var(--color-danger)]"

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <span className={cn("text-4xl font-semibold font-mono", scoreColor)}>
          {result.score}
        </span>
        <div>
          <p className="text-sm font-medium capitalize">{result.verdict}</p>
          <p className="text-xs text-[var(--color-muted)]">Safety Score</p>
        </div>
      </div>

      {result.issues.length > 0 && (
        <div className="space-y-2">
          {result.issues.map((issue, i) => (
            <div
              key={i}
              className="flex items-start gap-2 text-xs p-2 rounded-lg bg-white/[0.02]"
            >
              <Badge
                variant="outline"
                className={cn(
                  "text-[10px] shrink-0",
                  SEVERITY_STYLE[issue.severity] ?? "",
                )}
              >
                {issue.severity}
              </Badge>
              <span className="text-[var(--color-foreground)]">
                {issue.message}
              </span>
              {issue.file && (
                <span className="ml-auto font-mono text-[var(--color-muted)] shrink-0">
                  {issue.file}
                  {issue.line ? `:${issue.line}` : ""}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
