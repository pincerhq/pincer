import { useNavigate } from "react-router-dom"
import type { SkillInfo } from "@/api/types"
import { SkillCard } from "./SkillCard"
import { Skeleton } from "@/components/ui/skeleton"

interface SkillGridProps {
  skills: SkillInfo[]
  loading?: boolean
  viewMode?: "grid" | "list"
  onDelete?: (name: string) => void
}

export function SkillGrid({ skills, loading, viewMode = "grid", onDelete }: SkillGridProps) {
  const navigate = useNavigate()

  if (loading) {
    return viewMode === "list" ? (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-14 w-full bg-white/[0.06] rounded-xl" />
        ))}
      </div>
    ) : (
      <div className="grid grid-cols-4 gap-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-32 bg-white/[0.06] rounded-xl" />
        ))}
      </div>
    )
  }

  if (!skills.length) return null

  const handleClick = (skill: SkillInfo) =>
    skill.source === "integration" && skill.slug
      ? () => navigate(`/skills/integrations/${skill.slug}`)
      : undefined

  return viewMode === "list" ? (
    <div className="rounded-xl border border-[var(--color-border)] divide-y divide-[var(--color-border)] overflow-hidden">
      {skills.map((skill) => (
        <SkillCard
          key={skill.name}
          skill={skill}
          viewMode="list"
          onDelete={onDelete}
          onClick={handleClick(skill)}
        />
      ))}
    </div>
  ) : (
    <div className="grid grid-cols-4 gap-4">
      {skills.map((skill) => (
        <SkillCard
          key={skill.name}
          skill={skill}
          viewMode="grid"
          onDelete={onDelete}
          onClick={handleClick(skill)}
        />
      ))}
    </div>
  )
}
