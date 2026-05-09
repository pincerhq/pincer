import type { SkillInfo } from "@/api/types"
import { cn } from "@/lib/utils"
import { Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"

interface SkillCardProps {
  skill: SkillInfo
  viewMode?: "grid" | "list"
  onDelete?: (name: string) => void
  onClick?: () => void
}

function scoreColor(score: number) {
  if (score >= 80) return "text-[var(--color-success)]"
  if (score >= 50) return "text-[var(--color-warning)]"
  return "text-[var(--color-danger)]"
}

function scoreDot(score: number) {
  if (score >= 80) return "bg-[var(--color-success)]"
  if (score >= 50) return "bg-[var(--color-warning)]"
  return "bg-[var(--color-danger)]"
}

function SourceBadge({ source }: { source?: string }) {
  if (source === "mcp") {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">
        MCP
      </span>
    )
  }
  if (source === "integration") {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-400">
        INT
      </span>
    )
  }
  return null
}

function StatusDot({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "text-[10px] font-medium uppercase tracking-wider",
        status === "active"
          ? "text-[var(--color-success)]"
          : status === "error"
            ? "text-[var(--color-danger)]"
            : "text-[var(--color-muted)]",
      )}
    >
      {status}
    </span>
  )
}

export function SkillCard({ skill, viewMode = "grid", onDelete, onClick }: SkillCardProps) {
  const isFile = !skill.source || skill.source === "file"

  if (viewMode === "list") {
    return (
      <div
        className={cn(
          "flex items-center gap-4 px-4 py-3 bg-[var(--color-card)] transition-colors group",
          onClick ? "hover:bg-white/[0.04] cursor-pointer" : "hover:bg-white/[0.02]",
        )}
        onClick={onClick}
      >
        {/* Badge */}
        <div className="shrink-0 w-10 flex justify-center">
          {isFile ? (
            <div className="flex items-center gap-1">
              <div className={cn("h-1.5 w-1.5 rounded-full", scoreDot(skill.safety_score))} />
              <span className={cn("text-xs font-mono font-medium", scoreColor(skill.safety_score))}>
                {skill.safety_score}
              </span>
            </div>
          ) : (
            <SourceBadge source={skill.source} />
          )}
        </div>

        {/* Name + version */}
        <div className="w-44 shrink-0">
          <p className="text-sm font-medium truncate">{skill.name}</p>
          <p className="text-[11px] text-[var(--color-muted)] font-mono">
            {isFile ? `v${skill.version}` : skill.version}
          </p>
        </div>

        {/* Description */}
        <p className={cn(
          "flex-1 text-xs text-[var(--color-muted)] truncate",
          skill.source === "mcp" && "font-mono",
        )}>
          {skill.description}
        </p>

        {/* Status + delete */}
        <div className="flex items-center gap-3 shrink-0">
          <StatusDot status={skill.status} />
          {onDelete && isFile && (
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => { e.stopPropagation(); onDelete(skill.name) }}
              className="h-7 w-7 p-0 opacity-0 group-hover:opacity-100 transition-opacity text-[var(--color-muted)] hover:text-[var(--color-danger)]"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
    )
  }

  // Grid layout
  return (
    <div
      className={cn(
        "rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-4 transition-colors group",
        onClick
          ? "hover:border-[var(--color-accent)]/50 cursor-pointer"
          : "hover:border-[var(--color-border-hover)]",
      )}
      onClick={onClick}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium truncate">{skill.name}</h3>
          <p className="text-xs text-[var(--color-muted)] mt-0.5 font-mono">
            {isFile ? `v${skill.version}` : skill.version}
          </p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {isFile ? (
            <>
              <div className={cn("h-2 w-2 rounded-full", scoreDot(skill.safety_score))} />
              <span className={cn("text-xs font-mono font-medium", scoreColor(skill.safety_score))}>
                {skill.safety_score}
              </span>
            </>
          ) : (
            <SourceBadge source={skill.source} />
          )}
        </div>
      </div>

      <p className={cn(
        "text-xs text-[var(--color-muted)] mt-2 line-clamp-2",
        skill.source === "mcp" && "font-mono break-all",
      )}>
        {skill.description}
      </p>

      <div className="flex items-center justify-between mt-3">
        <StatusDot status={skill.status} />
        {onDelete && isFile && (
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); onDelete(skill.name) }}
            className="h-7 w-7 p-0 opacity-0 group-hover:opacity-100 transition-opacity text-[var(--color-muted)] hover:text-[var(--color-danger)]"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  )
}
