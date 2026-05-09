import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { SkillGrid } from "@/components/skills/SkillGrid"
import { InstallModal } from "@/components/skills/InstallModal"
import { useSkills, useSkillDelete } from "@/api/hooks/useSkills"
import { Button } from "@/components/ui/button"
import { Plus, RefreshCw, LayoutGrid, List } from "lucide-react"
import { cn } from "@/lib/utils"
import { toast } from "sonner"
import type { SkillInfo } from "@/api/types"

type Filter = "all" | "skills" | "integrations" | "mcp"
type ViewMode = "grid" | "list"

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "skills", label: "Skills" },
  { id: "integrations", label: "Integrations" },
  { id: "mcp", label: "MCP" },
]

function sourceOf(s: SkillInfo): Filter {
  if (s.source === "integration") return "integrations"
  if (s.source === "mcp") return "mcp"
  return "skills"
}

export function SkillsPage() {
  const [installOpen, setInstallOpen] = useState(false)
  const [filter, setFilter] = useState<Filter>("all")
  const [view, setView] = useState<ViewMode>("grid")

  const { data, isLoading, refetch } = useSkills()
  const deleteMutation = useSkillDelete()

  const all = data?.skills ?? []
  const visible = filter === "all" ? all : all.filter((s) => sourceOf(s) === filter)

  const counts = {
    all: all.length,
    skills: all.filter((s) => sourceOf(s) === "skills").length,
    integrations: all.filter((s) => sourceOf(s) === "integrations").length,
    mcp: all.filter((s) => sourceOf(s) === "mcp").length,
  }

  const handleDelete = (name: string) => {
    if (!confirm(`Delete skill "${name}"?`)) return
    deleteMutation.mutate(name, {
      onSuccess: () => toast.success(`Deleted ${name}`),
      onError: () => toast.error(`Failed to delete ${name}`),
    })
  }

  // When "All" is selected split into sections; otherwise flat list
  const fileSkills = visible.filter((s) => sourceOf(s) === "skills")
  const integrationSkills = visible.filter((s) => sourceOf(s) === "integrations")
  const mcpSkills = visible.filter((s) => sourceOf(s) === "mcp")
  const useSections = filter === "all"

  return (
    <PageContainer title="Extensions">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-5">
        {/* Filter tabs */}
        <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                filter === f.id
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              {f.label}
              {counts[f.id] > 0 && (
                <span
                  className={cn(
                    "text-[10px] font-mono rounded px-1 py-px",
                    filter === f.id
                      ? "bg-white/[0.1] text-[var(--color-foreground)]"
                      : "bg-white/[0.04] text-[var(--color-muted)]",
                  )}
                >
                  {counts[f.id]}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Right controls */}
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
          <Button
            size="sm"
            onClick={() => setInstallOpen(true)}
            className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
          >
            <Plus className="h-3.5 w-3.5 mr-1.5" />
            Install
          </Button>
        </div>
      </div>

      {/* Content */}
      {useSections ? (
        <div className="space-y-8">
          {(isLoading || fileSkills.length > 0) && (
            <Section label="Skills" count={fileSkills.length} loading={isLoading}>
              <SkillGrid skills={fileSkills} loading={isLoading} viewMode={view} onDelete={handleDelete} />
            </Section>
          )}
          {(isLoading || integrationSkills.length > 0) && (
            <Section label="Integrations" count={integrationSkills.length} loading={isLoading}>
              <SkillGrid skills={integrationSkills} loading={isLoading} viewMode={view} />
            </Section>
          )}
          {(isLoading || mcpSkills.length > 0) && (
            <Section label="MCP Servers" count={mcpSkills.length} loading={isLoading}>
              <SkillGrid skills={mcpSkills} loading={isLoading} viewMode={view} />
            </Section>
          )}
          {!isLoading && all.length === 0 && <EmptyState />}
        </div>
      ) : (
        <SkillGrid
          skills={visible}
          loading={isLoading}
          viewMode={view}
          onDelete={handleDelete}
        />
      )}

      <InstallModal open={installOpen} onClose={() => setInstallOpen(false)} />
    </PageContainer>
  )
}

function Section({
  label,
  count,
  loading,
  children,
}: {
  label: string
  count: number
  loading?: boolean
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)]">
          {label}
        </h2>
        {!loading && count > 0 && (
          <span className="text-[10px] font-mono text-[var(--color-muted)] bg-white/[0.04] px-1.5 py-px rounded">
            {count}
          </span>
        )}
      </div>
      {children}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-[var(--color-muted)]">
      <p className="text-sm">Nothing here yet</p>
      <p className="text-xs mt-1">Install skills or configure integrations to extend your agent</p>
    </div>
  )
}
