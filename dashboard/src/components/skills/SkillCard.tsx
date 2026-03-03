import type { SkillInfo } from "@/api/types"
import { cn } from "@/lib/utils"
import { Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"

interface SkillCardProps {
  skill: SkillInfo
  onDelete?: (name: string) => void
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

export function SkillCard({ skill, onDelete }: SkillCardProps) {
  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-4 hover:border-[var(--color-border-hover)] transition-colors group">
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium truncate">{skill.name}</h3>
          <p className="text-xs text-[var(--color-muted)] mt-0.5 font-mono">
            v{skill.version}
          </p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <div className={cn("h-2 w-2 rounded-full", scoreDot(skill.safety_score))} />
          <span className={cn("text-xs font-mono font-medium", scoreColor(skill.safety_score))}>
            {skill.safety_score}
          </span>
        </div>
      </div>

      <p className="text-xs text-[var(--color-muted)] mt-2 line-clamp-2">
        {skill.description}
      </p>

      <div className="flex items-center justify-between mt-3">
        <span
          className={cn(
            "text-[10px] font-medium uppercase tracking-wider",
            skill.status === "active"
              ? "text-[var(--color-success)]"
              : skill.status === "error"
                ? "text-[var(--color-danger)]"
                : "text-[var(--color-muted)]",
          )}
        >
          {skill.status}
        </span>
        {onDelete && (
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => {
              e.stopPropagation()
              onDelete(skill.name)
            }}
            className="h-7 w-7 p-0 opacity-0 group-hover:opacity-100 transition-opacity text-[var(--color-muted)] hover:text-[var(--color-danger)]"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  )
}
