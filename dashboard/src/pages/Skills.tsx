import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { SkillGrid } from "@/components/skills/SkillGrid"
import { useSkills } from "@/api/hooks/useSkills"
import { Button } from "@/components/ui/button"
import { RefreshCw, LayoutGrid, List } from "lucide-react"
import { cn } from "@/lib/utils"

type ViewMode = "grid" | "list"

export function SkillsPage() {
  const [view, setView] = useState<ViewMode>("grid")

  const { data, isLoading, refetch } = useSkills()
  const skills = data?.skills ?? []

  return (
    <PageContainer title="Skills">
      {/* Toolbar */}
      <div className="flex items-center justify-end mb-5">
        <div className="flex items-center gap-2">
          {/* View toggle */}
          <div className="flex items-center rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1">
            <button
              onClick={() => setView("grid")}
              title="Grid view"
              className={cn(
                "p-1.5 rounded-md transition-colors",
                view === "grid"
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <LayoutGrid className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setView("list")}
              title="List view"
              className={cn(
                "p-1.5 rounded-md transition-colors",
                view === "list"
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <List className="h-3.5 w-3.5" />
            </button>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            className="border-[var(--color-border)]"
          >
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
            Refresh
          </Button>
        </div>
      </div>

      {/* Content */}
      <SkillGrid skills={skills} loading={isLoading} viewMode={view} />
      {!isLoading && skills.length === 0 && <EmptyState />}
    </PageContainer>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-[var(--color-muted)]">
      <p className="text-sm">No skills yet</p>
      <p className="text-xs mt-1">
        Add a directory with a SKILL.md file under <code>skills/</code> or <code>~/.pincer/skills/</code> and restart
      </p>
    </div>
  )
}
