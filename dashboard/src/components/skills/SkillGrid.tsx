import type { SkillInfo } from "@/api/types"
import { SkillCard } from "./SkillCard"
import { Skeleton } from "@/components/ui/skeleton"

interface SkillGridProps {
  skills: SkillInfo[]
  loading?: boolean
  onDelete?: (name: string) => void
}

export function SkillGrid({ skills, loading, onDelete }: SkillGridProps) {
  if (loading) {
    return (
      <div className="grid grid-cols-4 gap-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-32 bg-white/[0.06] rounded-xl" />
        ))}
      </div>
    )
  }

  if (!skills.length) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-[var(--color-muted)]">
        <p className="text-sm">No skills installed</p>
        <p className="text-xs mt-1">Install skills to extend your agent</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-4 gap-4">
      {skills.map((skill) => (
        <SkillCard key={skill.name} skill={skill} onDelete={onDelete} />
      ))}
    </div>
  )
}
